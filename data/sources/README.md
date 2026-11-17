# TrackWise source records

`trackwise_records.csv` is the source export used by the local MATCH backend. It is versioned using Git LFS. Install Git LFS before committing and run `git lfs pull` after cloning so the working copy contains CSV data rather than an LFS pointer.

- Origin: accepted-record export of `GTW_Complete_1-10_sample.csv`.
- Records: 19,643 accepted source records, retaining original column names and field text.
- Encoding: UTF-8 with BOM, comma-separated CSV.
- Size: 121,397,555 bytes.
- SHA-256: `475eb66c11a4af2b506aab869cd20b88748243d896e8414affc59387ba74d840`.

The original import contained 19,644 rows: 19,643 were accepted and one duplicate ID was held in the local database quarantine. This CSV is the existing accepted-record export, not the raw file containing that duplicate. Renaming it did not change its contents. The existing local database retains the original import metadata and quarantine.

The readable filename is stable. Generated database snapshots keep content-based IDs for provenance and remain ignored, as do generated caches and new index generations. The pinned runtime model and verified reference index archive are supplied under `data/runtime/`. After replacing this source, use the launcher to import it rather than manually editing a database. A new import has its own snapshot identity.

From the backend repository root, initialize the source on a new machine with:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\Run-Match.ps1
```

Use `-Source "C:\data\export.xlsx" -Sheet "Sheet1"` for another workbook. The command also prepares the model/index and verifies real API searches. The included CSV is repository data, not a package asset in the Python wheel. For Databricks, provision it into data storage separately from the app-source upload as described in [deployment preparation](../../DEPLOYMENT.md).
