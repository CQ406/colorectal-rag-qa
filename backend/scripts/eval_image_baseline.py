"""Reproducible engineering sanity checks; never labels clinical image pairs."""
import argparse
import io
import json
from datetime import datetime, timezone
from PIL import Image
from sqlalchemy import select
from app.core.config import PROJECT_ROOT
from app.db.session import session_scope
from app.models.knowledge import ImageAsset
from app.repositories import image_repo
from app.services.image_features import FEATURE_VERSION, extract_feature


def png(image):
    stream = io.BytesIO()
    image.save(stream,format='PNG')
    return stream.getvalue()


def cosine(left,right):
    return sum(a*b for a,b in zip(left,right))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--backfill',action='store_true',help='Recompute unknown or older features using the current version')
    args = parser.parse_args()
    red = extract_feature(png(Image.new('RGB',(32,32),'red')))
    green = extract_feature(png(Image.new('RGB',(32,32),'green')))
    resized = extract_feature(png(Image.new('RGB',(64,64),'red')))
    ordered = Image.new('RGB',(32,32),'red')
    ordered.paste('green',(16,0,32,32))
    shuffled = ordered.rotate(90)
    checks = {'same_image_resize_cosine':cosine(red,resized), 'distinct_color_cosine':cosine(red,green),
        'rearranged_pixels_same_histogram_cosine':cosine(extract_feature(png(ordered)),extract_feature(png(shuffled)))}
    assert abs(checks['same_image_resize_cosine']-1)<1e-6
    assert abs(checks['distinct_color_cosine'])<1e-6
    assert abs(checks['rearranged_pixels_same_histogram_cosine']-1)<1e-6
    samples = []
    with session_scope() as db:
        assets = list(db.scalars(select(ImageAsset).order_by(ImageAsset.created_at)))
        if args.backfill:
            for asset in assets:
                if image_repo.ensure_feature(db,asset) is None:
                    raise RuntimeError(f'Feature backfill failed: {asset.id}')
        for asset in assets:
            data = image_repo.read_bytes(asset)
            feature = extract_feature(data)
            with Image.open(io.BytesIO(data)) as image:
                resized_data = png(image.convert('RGB').resize((max(1,image.width//2),max(1,image.height//2))))
            similar = image_repo.similar_images(db,feature,top_k=3,exclude_id=str(asset.id))
            samples.append({'image_id':str(asset.id),'published':asset.is_public,'feature_model':asset.feature_model,
                'feature_dimension':len(feature),'resize_cosine':cosine(feature,extract_feature(resized_data)),
                'public_neighbors':[{'id':str(row.id),'cosine':score} for row,score in similar]})
    report = {'created_at':datetime.now(timezone.utc).isoformat(),'feature_model':FEATURE_VERSION,
        'engineering_checks':checks, 'samples':samples,
        'clinical_labeled_pairs':0,'clinical_validation_status':'not_evaluated_no_independent_labels',
        'upgrade_decision':'Retain reproducible baseline. No evidence yet to claim an encoder upgrade improves clinical similarity.',
        'limitations':['Histogram discards spatial anatomy: rearranging pixels preserves cosine=1.',
            'Only three stored images; one published. No trustworthy clinical precision/recall can be estimated.',
            'Different encoder versions require separate columns/indexes and full recomputation before switching.']}
    target = PROJECT_ROOT/'data/eval/image_baseline_20261104.json'
    target.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False),flush=True)


if __name__ == '__main__':
    main()
