# Este script sirve de ejemplo para la interfaz web.
# Puedes cambiar parámetros desde la UI y usar un dataset CSV.

import argparse
import json
from pathlib import Path

PARAMS = {
    "multiplier": {"default": 2, "type": "float"},
    "threshold": {"default": 50, "type": "int"},
    "label": {"default": "demo", "type": "string"},
}


def main():
    parser = argparse.ArgumentParser(description="Ejemplo de script configurable")
    parser.add_argument("--multiplier", type=float, default=2.0)
    parser.add_argument("--threshold", type=int, default=50)
    parser.add_argument("--label", default="demo")
    parser.add_argument("--dataset", default="")
    args = parser.parse_args()

    result = {
        "script": "sample_script.py",
        "multiplier": args.multiplier,
        "threshold": args.threshold,
        "label": args.label,
        "dataset": args.dataset,
        "status": "ok",
    }

    if args.dataset and Path(args.dataset).exists():
        try:
            import pandas as pd
            df = pd.read_csv(args.dataset)
            result["rows"] = len(df)
            result["columns"] = list(df.columns)
            result["preview"] = df.head(5).to_dict(orient="records")
        except Exception as exc:
            result["dataset_error"] = str(exc)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
