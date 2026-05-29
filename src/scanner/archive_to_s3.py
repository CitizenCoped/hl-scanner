import datetime as dt
import os
from pathlib import Path

import boto3

BUCKET = os.environ["S3_BUCKET"]
ROOT = Path("/opt/scanner/data")
s3 = boto3.client("s3", region_name="ap-northeast-1")
cutoff = (dt.date.today() - dt.timedelta(days=30)).isoformat()


def main() -> None:
    for table in ("bars", "bbo", "trades"):
        for path in (ROOT / table).glob("coin=*/dt=*/*.parquet"):
            dt_part = next((seg for seg in path.parts if seg.startswith("dt=")), None)
            if not dt_part:
                continue
            if dt_part[3:] < cutoff:
                key = "cold/" + str(path.relative_to(ROOT))
                s3.upload_file(
                    str(path),
                    BUCKET,
                    key,
                    ExtraArgs={"StorageClass": "STANDARD"},
                )
                path.unlink()


if __name__ == "__main__":
    main()
