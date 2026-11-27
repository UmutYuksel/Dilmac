"""Writes DilmacCore's English word list from pyspellchecker's dictionary (the one pc/textutil.py's Lexicon uses),
so the iPhone decides "is this an English word" exactly like the Windows app.

Usage:  ios/.venv/bin/pip install pyspellchecker==0.9.1 && ios/.venv/bin/python ios/scripts/make-wordlist.py
Output: ios/DilmacCore/Sources/DilmacCore/Resources/en-words.deflate (raw DEFLATE of the sorted words, one per line;
Foundation's NSData.decompressed(using: .zlib) reads it). pyspellchecker is MIT licensed.
"""
import gzip
import json
import os
import zlib
from pathlib import Path

import spellchecker

source = Path(spellchecker.__file__).parent / "resources" / "en.json.gz"
words = sorted(json.load(gzip.open(source)))
data = "\n".join(words).encode("utf-8")
packer = zlib.compressobj(9, zlib.DEFLATED, -15)
out = Path(__file__).resolve().parent.parent / "DilmacCore/Sources/DilmacCore/Resources/en-words.deflate"
out.write_bytes(packer.compress(data) + packer.flush())
print(f"{len(words)} words, {out.stat().st_size} bytes -> {out}")
