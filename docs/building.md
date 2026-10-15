# Rebuild release artifacts

The runtime package has no third-party dependencies. The PEP 517 build backend is
pinned in pyproject.toml. The release artifact workflow uses Python 3.12 and the
same SOURCE_DATE_EPOCH as this command:

```sh
python -m pip install build
SOURCE_DATE_EPOCH=1792018800 python -m build
python scripts/audit_release.py dist/*.whl dist/*.tar.gz
cd dist
sha256sum *.whl *.tar.gz > SHA256SUMS
```

Build twice from the same source revision and compare SHA-256 hashes of both wheel
and source distribution. The alpha artifacts were rebuilt twice and compared byte
for byte. The wheel was installed in a new environment outside a Git checkout,
then its demo and the same 65 boundary tests ran against that installed package.

The content audit checks archive entries for excluded runtime directories, native
credential files, private source paths, tokens, keys and links. It complements
source provenance review; pattern matching alone is not proof that arbitrary
content is public. The dashboard screenshot uses synthetic observations only.
Raw native qualification evidence and operator state are excluded.

GitHub Actions also exercises Python 3.11, 3.12 and 3.13. Those jobs test package
behavior and resource installation; only the declared release build environment is
the artifact reproducibility target. No PyPI upload is required.
