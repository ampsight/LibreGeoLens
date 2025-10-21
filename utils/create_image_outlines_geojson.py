import rasterio
from rasterio.warp import transform
import os
import json
import boto3
import argparse
from datetime import datetime
from tqdm import tqdm
import logging
from botocore import UNSIGNED
from botocore.client import Config
from botocore.exceptions import ClientError, NoCredentialsError
from rasterio.session import AWSSession

logging.getLogger("botocore").setLevel(logging.WARNING)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

def _have_aws_credentials() -> bool:
    try:
        sess = boto3.Session()
        return sess.get_credentials() is not None
    except Exception:
        return False

def parse_s3_path(s3_path):
    """ Parse S3 path into bucket and prefix. """
    if not s3_path.startswith("s3://"):
        raise ValueError("S3 path must start with 's3://'")
    parts = s3_path[5:].split("/", 1)
    bucket = parts[0]
    prefix = parts[1] if len(parts) > 1 else ""
    return bucket, prefix


def list_files_in_s3_directory(s3_directory, file_extensions=None):
    """Recursively list files in an S3 prefix and filter by extension.
       - If creds are available, use signed requests.
       - If not, use UNSIGNED (public) requests.
       - If a single object path is provided, return it directly.
       - If bucket denies ListBucket, log and continue.
    """
    bucket, prefix = parse_s3_path(s3_directory)

    # If the "directory" is a specific file, just return it
    if prefix and file_extensions and prefix.lower().endswith(tuple(file_extensions)):
        return [f"s3://{bucket}/{prefix}"]

    # Decide signed vs. unsigned automatically
    session = boto3.Session()
    region = os.getenv("AWS_DEFAULT_REGION") or "us-east-1"
    if _have_aws_credentials():
        s3 = session.client("s3", region_name=region)
    else:
        s3 = session.client("s3", config=Config(signature_version=UNSIGNED), region_name=region)

    files = []
    paginator = s3.get_paginator("list_objects_v2")
    try:
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith("/"):
                    continue
                if file_extensions and not any(key.lower().endswith(ext) for ext in file_extensions):
                    continue
                files.append(f"s3://{bucket}/{key}")
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        # Public buckets often block anonymous ListBucket; private buckets may block it too.
        logger.warning(
            f"Could not list '{s3_directory}' (Error: {code}). "
            f"If you meant a single file, pass the full .tif key; "
            f"otherwise ensure your IAM policy allows ListBucket for that prefix."
        )
    except NoCredentialsError:
        # Should not happen because we choose UNSIGNED when no creds, but handle anyway.
        logger.warning(
            "No AWS credentials found for listing and UNSIGNED path failed. "
            "Pass exact object keys or configure credentials."
        )

    return files

def extract_geocoordinates_rasterio(file_path, target_crs="EPSG:4326"):
    try:
        # If you have creds, use them via AWSSession. If not, open anonymously using AWS_NO_SIGN_REQUEST inside the Env.
        if _have_aws_credentials():
            aws = AWSSession(boto3.Session())  # picks up your configured credentials/profile
            env_kwargs = {"session": aws}
        else:
            # Anonymous reads, but only within this context (no shell env var needed)
            env_kwargs = {"AWS_NO_SIGN_REQUEST": "YES", "AWS_REGION": os.getenv("AWS_DEFAULT_REGION", "us-east-1")}

        with rasterio.Env(**env_kwargs):
            with rasterio.open(file_path) as src:
                bounds = src.bounds
                crs = src.crs
                if crs is None:
                    logger.error(f"The GeoTIFF {file_path} does not have a defined CRS. Skipping this file.")
                    return None

                corners = [
                    (bounds.left, bounds.top),     # TL
                    (bounds.right, bounds.top),    # TR
                    (bounds.right, bounds.bottom), # BR
                    (bounds.left, bounds.bottom),  # BL
                    (bounds.left, bounds.top)      # close
                ]

                x_coords, y_coords = zip(*corners)
                x_reproj, y_reproj = transform(crs, target_crs, x_coords, y_coords)
                return list(zip(x_reproj, y_reproj))

    except Exception as e:
        logger.error(f"An error occurred while processing {file_path}: {e}")
        return None


def geojson_conversion(image_paths):
    geojson = {
        "type": "FeatureCollection",
        "features": [],
    }

    with tqdm(total=len(image_paths), desc="Processing S3 paths", unit="file") as pbar:
        for image_path in image_paths:
            if image_path.endswith(".tif"):
                polygon = extract_geocoordinates_rasterio(image_path)
            else:
                logger.error(f"Unsupported file type: {image_path}")
                pbar.update(1)
                continue

            if not polygon:
                logger.error(f"Could not extract geocoordinates for {image_path}")
                pbar.update(1)
                continue

            feature = {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [polygon],
                },
                "properties": {
                    "remote_path": image_path
                },
            }
            geojson["features"].append(feature)
            pbar.update(1)

    return geojson


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Creates .geojson with COG imagery outlines and remote paths from data in S3 to use with LibreGeoLens."
    )

    parser.add_argument(
        "--s3_directories",
        nargs='+',
        required=True,
        help="List of S3 directories to process. Example: s3://bucket1/path/to/dir1/ s3://bucket2/path/to/dir2/"
    )
    parser.add_argument(
        "--out_dir",
        type=str,
        required=False,
        default=".",
        help="Output directory where the .geojson will be saved to."
    )

    args = parser.parse_args()

    s3_paths = []
    for directory in args.s3_directories:
        logger.info(f"Processing directory: {directory}")
        files = list_files_in_s3_directory(directory, [".tif"])
        s3_paths.extend(files)

    geojson_data = geojson_conversion(s3_paths)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    unique_filename = f"imagery_{timestamp}.geojson"

    os.makedirs(args.out_dir, exist_ok=True)
    out_path = os.path.join(args.out_dir, unique_filename)
    logger.info(f"Saving GeoJSON file to {out_path}")
    with open(out_path, "w") as f:
        json.dump(geojson_data, f, indent=4)
