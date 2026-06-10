#!/usr/bin/env python3
import os
import urllib.request
import argparse

MODELS = {
    "en_US-lessac-medium": {
        "onnx": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium/en_US-lessac-medium.onnx",
        "json": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json"
    },
    "en_US-ryan-high": {
        "onnx": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/ryan/high/en_US-ryan-high.onnx",
        "json": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/ryan/high/en_US-ryan-high.onnx.json"
    },
    "en_GB-alan-medium": {
        "onnx": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_GB/alan/medium/en_GB-alan-medium.onnx",
        "json": "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_GB/alan/medium/en_GB-alan-medium.onnx.json"
    }
}

def download_file(url, dest):
    if os.path.exists(dest):
        print(f"Already exists: {dest}")
        return
    print(f"Downloading {url} to {dest}...")
    urllib.request.urlretrieve(url, dest)
    print("Done.")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=str, default=os.path.join(os.path.dirname(__file__), "..", "data", "piper_models"))
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    for name, urls in MODELS.items():
        print(f"=== {name} ===")
        onnx_dest = os.path.join(args.output_dir, f"{name}.onnx")
        json_dest = os.path.join(args.output_dir, f"{name}.onnx.json")
        download_file(urls["onnx"], onnx_dest)
        download_file(urls["json"], json_dest)

if __name__ == "__main__":
    main()
