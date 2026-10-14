# Publishing CancelLens

The release workflow publishes wheel and sdist to PyPI using GitHub Actions OIDC.
It does not require a stored PyPI API token. Publishing is triggered by a **published
GitHub Release**, after the reusable CI workflow and package checks pass.

## Configure the publisher

In the GitHub repository settings, create an Actions environment named `pypi`. Configure
its allowed release tags and, if desired, required reviewers. The publishing job declares
this environment; use the identical name in PyPI's publisher form.

For a new PyPI project, open [account publishing settings](https://pypi.org/manage/account/publishing/)
and add a pending GitHub publisher with these values:

- **PyPI Project Name:** `cancellens`
- **Owner:** `0then0`
- **Repository name:** `cancellens`
- **Workflow name:** `publish.yml`
- **Environment name:** `pypi`

`Workflow name` is the filename, without `.github/workflows/`. It is not the workflow's
display name. A pending publisher creates the PyPI project on its first successful
upload and does not reserve the name beforehand. For an existing project, add the
same publisher under that project's publishing settings.

Official references: [adding a publisher](https://docs.pypi.org/trusted-publishers/adding-a-publisher/),
[creating a project](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/),
and [publishing with OIDC](https://docs.pypi.org/trusted-publishers/using-a-publisher/).

## Release procedure

1. Update `project.version` in `pyproject.toml`, `__version__` and JSON `package_version`
   in the package. Update versioned wheel paths in validation documentation/workflows
   and add a changelog entry. For the first release these values are already `0.1.0`.
2. Run the checks in [CONTRIBUTING.md](../CONTRIBUTING.md), including wheel smoke and
   the aiosqlite case. Record which interpreters and integrations were checked.
3. Commit and push the release changes. Wait for the `checks` workflow to pass.
4. Create a GitHub Release with a tag matching the package version, such as `v0.1.0`,
   at the checked commit. Publishing a draft release triggers `publish.yml`.
5. Approve the `pypi` environment deployment if approval is configured. Check the
   publish job and the resulting [PyPI project](https://pypi.org/project/cancellens/).

The publishing workflow validates the release tag, runs the full Python/OS matrix and
aiosqlite acceptance, builds distributions, checks their metadata and README with
Twine, and tests the installed wheel. Only the upload job receives `id-token: write`.
It downloads the tested build artifacts and does not execute source/build code with
publishing permissions. The publishing action also produces distribution attestations.

PyPI release files cannot be overwritten. Fix an incorrect release by publishing a
new version; use PyPI's yank feature when an existing version should not be selected
by default. Pushing commits or creating a draft release does not upload a package.
