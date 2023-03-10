#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import argparse
import logging
from pathlib import Path
import shutil
from tempfile import NamedTemporaryFile
from typing import Optional, Tuple

import pandas as pd
import torchaudio
from examples.speech_to_text.data_utils import (
    create_zip,
    extract_fbank_features,
    filter_manifest_df,
    gen_config_yaml,
    gen_vocab,
    get_zip_manifest,
    load_df_from_tsv,
    save_df_to_tsv,
)
from torch import Tensor
from torch.utils.data import Dataset
from torchaudio.datasets.utils import download_url, extract_archive
from tqdm import tqdm


log = logging.getLogger(__name__)


MANIFEST_COLUMNS = ["id", "audio", "n_frames", "tgt_text", "speaker"]


class ZambeziVoice(Dataset):
    """Create a Dataset for CoVoST (https://github.com/facebookresearch/covost).

    Args:
        root (str): root path to the dataset and generated manifests/features
        source_language (str): source (audio) language
        target_language (str, optional): target (text) language,
        None for no translation (default: None)
        version (int, optional): CoVoST version. (default: 2)
        download (bool, optional): Whether to download the dataset if it is not
        found at root path. (default: ``False``).
    """

    VERSIONS = {2}
    SPLITS = ["train", "dev", "test"]

    XX_EN_LANGUAGES = {
        1: [ "nya", "toi", "loz"],
        2: [
            "nya",
            "toi",
            "loz"
        ],
    }
    EN_XX_LANGUAGES = {
        1: [],
        2: [
            "nya",
            "toi",
            "loz"
        ],
    }

    def __init__(
        self,
        root: str,
        split: str,
        source_language: str,
        target_language: Optional[str] = None,
        version: int = 2,
    ) -> None:
        assert version in self.VERSIONS and split in self.SPLITS
        assert source_language is not None
        self.no_translation = target_language is None
        if not self.no_translation:
            assert "en" in {source_language, target_language}
            if source_language == "en":
                assert target_language in self.EN_XX_LANGUAGES[version]
            else:
                assert source_language in self.XX_EN_LANGUAGES[version]
        else:
            # Hack here so that we can get "split" column from CoVoST TSV.
            # Note that we use CoVoST train split for ASR which is an extension
            # to Common Voice train split.
            target_language = "nya" if source_language == "en" else "en"

        self.root: Path = Path(root)

        if split == "train":
            train_tsv_path = self.root / f"{split}.tsv"
            assert train_tsv_path.is_file()
            df = load_df_from_tsv(train_tsv_path)
            df = df[["audio_id", "sentence","speaker_name"]]
        elif split == "dev":
            dev_tsv_path = self.root / f"{split}.tsv"
            assert dev_tsv_path.is_file()
            df = load_df_from_tsv(dev_tsv_path)
            df = df[["audio_id", "sentence","speaker_name"]]
        elif split == "test":
            test_tsv_path = self.root / f"{split}.tsv"
            assert test_tsv_path.is_file()
            df = load_df_from_tsv(test_tsv_path)
            df = df[["audio_id", "sentence","speaker_name"]]

        data = df.to_dict(orient="index").items()
        data = [v for k, v in sorted(data, key=lambda x: x[0])]
        self.data = []
        for e in data:
          try:
              path = self.root / "audio" / e["audio_id"]
              _ = torchaudio.info(path.as_posix())
              self.data.append(e)
          except RuntimeError:
              pass

    def __getitem__(
        self, n: int
    ) -> Tuple[Tensor, int, str, str, Optional[str], str, str]:
        """Load the n-th sample from the dataset.

        Args:
            n (int): The index of the sample to be loaded

        Returns:
            tuple: ``(waveform, sample_rate, sentence, translation, speaker_id,
            sample_id)``
        """
        data = self.data[n]
        path = self.root / "audio" / data["audio_id"]
        waveform, sample_rate = torchaudio.load(path)
        sentence = data["sentence"]
        speaker_id = data["speaker_name"]
        _id = data["audio_id"].replace(".wav", "")
        return waveform, sample_rate, sentence,speaker_id, _id

    def __len__(self) -> int:
        return len(self.data)


def process(args):
    root = Path(args.data_root).absolute() / args.src_lang
    if not root.is_dir():
        raise NotADirectoryError(f"{root} does not exist")
    # Extract features
    feature_root = root / "fbank80"
    feature_root.mkdir(exist_ok=True)
    for split in ZambeziVoice.SPLITS:
        print(f"Fetching split {split}...")
        dataset = ZambeziVoice(root, split, args.src_lang, args.tgt_lang)
        print("Extracting log mel filter bank features...")
        for waveform, sample_rate,_, _, utt_id in tqdm(dataset):
            extract_fbank_features(
                waveform, sample_rate, feature_root / f"{utt_id}.npy"
            )
    # Pack features into ZIP
    zip_path = root / "fbank80.zip"
    print("ZIPing features...")
    create_zip(feature_root, zip_path)
    print("Fetching ZIP manifest...")
    audio_paths, audio_lengths = get_zip_manifest(zip_path)
    # Generate TSV manifest
    print("Generating manifest...")
    train_text = []
    task = f"asr_{args.src_lang}"
    if args.tgt_lang is not None:
        task = f"st_{args.src_lang}_{args.tgt_lang}"
    for split in ZambeziVoice.SPLITS:
        manifest = {c: [] for c in MANIFEST_COLUMNS}
        dataset = ZambeziVoice(root, split, args.src_lang, args.tgt_lang)
        for _, _, src_utt, speaker_id, utt_id in tqdm(dataset):
            manifest["id"].append(utt_id)
            manifest["audio"].append(audio_paths[utt_id])
            manifest["n_frames"].append(audio_lengths[utt_id])
            manifest["tgt_text"].append(src_utt)
            manifest["speaker"].append(speaker_id)
        is_train_split = split.startswith("train")
        if is_train_split:
            train_text.extend(manifest["tgt_text"])
        df = pd.DataFrame.from_dict(manifest)
        df = filter_manifest_df(df, is_train_split=is_train_split)
        save_df_to_tsv(df, root / f"{split}_{task}.tsv")
    # Generate vocab
    vocab_size_str = "" if args.vocab_type == "char" else str(args.vocab_size)
    spm_filename_prefix = f"spm_{args.vocab_type}{vocab_size_str}_{task}"
    with NamedTemporaryFile(mode="w") as f:
        for t in train_text:
            f.write(t + "\n")
        gen_vocab(
            Path(f.name),
            root / spm_filename_prefix,
            args.vocab_type,
            args.vocab_size
        )
    # Generate config YAML
    gen_config_yaml(
        root,
        spm_filename=spm_filename_prefix + ".model",
        yaml_filename=f"config_{task}.yaml",
        specaugment_policy="lb",
    )
    # Clean up
    shutil.rmtree(feature_root)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data-root", "-d", required=True, type=str,
        help="data root with sub-folders for each language <root>/<src_lang>"
    )
    parser.add_argument(
        "--vocab-type",
        default="unigram",
        required=True,
        type=str,
        choices=["bpe", "unigram", "char"],
    ),
    parser.add_argument("--vocab-size", default=1000, type=int)
    parser.add_argument("--src-lang", "-s", required=True, type=str)
    parser.add_argument("--tgt-lang", "-t", type=str)
    args = parser.parse_args()

    process(args)


if __name__ == "__main__":
    main()