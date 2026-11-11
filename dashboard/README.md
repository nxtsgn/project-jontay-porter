# Private dashboard (claude.ai)

https://claude.ai/artifact/EJAd4JYHaQT9C4o3o5k9pT (private to the owner). It's a claude.ai "Data" artifact. Its page
(`index.html`, kept here as a reference copy) and 8 datasets live in the artifact's store. Each dataset is a CSV made by
`scripts/dashboard_export.py` from the `price-data` branch and uploaded as an asset.

Refresh by hand:

    git fetch origin price-data && mkdir -p /tmp/pd && git archive origin/price-data data | tar -x -C /tmp/pd
    python scripts/dashboard_export.py --data /tmp/pd/data --out /tmp/dash

Then upload each `/tmp/dash/<id>.csv` to the artifact and point `datasets/<id>` at the new asset URL.
