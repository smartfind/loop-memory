# Releasing `loop-memory`

The full release flow is **automated through GitHub Actions + PyPI
Trusted Publishing** — no API token ever has to live on disk. This page
documents every step so the very first release and subsequent ones look
the same.

## One-time bootstrap (per project, per environment)

1. **Create the PyPI project.** If the name is still available, head to
   <https://pypi.org/manage/projects/> and create `loop-memory`.
2. **Register the GitHub Actions Trusted Publisher** for the project.
   On PyPI: `loop-memory` → *Publishing* → *Add a new pending publisher*:
   - Owner: `smartfind`
   - Repository: `loop-memory`
   - Workflow filename: `publish.yml`
   - Environment name: `pypi`
3. **Add a GitHub environment named `pypi`** in repository settings. No
   protection rules are required, but the name must match so the
   workflow can scope its token correctly.

After this is done once, every subsequent release needs no PyPI
credentials.

## Cutting a release

1. Bump `version` in `pyproject.toml`.
2. Move the `[Unreleased]` block in `CHANGELOG.md` into a dated version
   section (`## [0.4.0] - YYYY-MM-DD`).
3. Land the change on `main` via PR and wait for `tests.yml` + the
   secret scanner to turn green.
4. Tag the merge commit:
   ```bash
   git switch main
   git pull --ff-only
   git tag -s v0.4.0 -m "loop-memory 0.4.0"
   git push origin v0.4.0
   ```
5. The `publish.yml` workflow will:
   - build sdist + wheel,
   - run `twine check`,
   - publish to PyPI via OIDC,
   - draft a GitHub release with auto-generated notes.

The first publish from a new GitHub environment needs a manual
*Approve* click in the GitHub Actions UI; subsequent publishes from the
same environment are automatic.

## Local dry-run + token fallback

`scripts/release.sh --tag v0.4.0` will:

- Verify the working tree is clean and on `main`.
- Confirm `pyproject.toml` version matches the tag.
- Run `ruff check .` and `pytest -q`.
- Build sdist + wheel into `dist/`.
- Run `twine check` (must report `PASSED` for every artefact).
- Refuse to upload — that's what GitHub Actions is for.

To actually upload **locally** (only needed for the very first release,
before the Trusted Publisher is registered), set:

```bash
export TWINE_USERNAME=__token__
export TWINE_PASSWORD=pypi-<your token>
./scripts/release.sh --upload
```

After the first successful upload the Trusted Publisher takes over and
local uploads can be disabled again.

## Verifying a release

```bash
pip index versions loop-memory          # should list 0.4.0
python -m pip install --upgrade loop-memory
python -c "import loop_memory; print(loop_memory.__version__)"
```
