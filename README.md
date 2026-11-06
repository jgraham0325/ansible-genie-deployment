# Ansible → Databricks Genie agent PoC

Deploys Genie agents (Genie spaces) from local JSON definitions into a Databricks
workspace, using only `ansible.builtin` modules against the Databricks REST API.

## Setup

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
source .venv/bin/activate

# The playbook reuses the CLI's OAuth login for the profile
databricks auth login --profile field-eng-east   # only if the profile isn't valid
```

## Usage

```bash
ansible-playbook playbooks/deploy_genie.yml --check   # dry run: what would change
ansible-playbook playbooks/deploy_genie.yml           # create / update agents
ansible-playbook playbooks/teardown_genie.yml         # move agents to trash
```

Add `-vvv` to see API request/response bodies. They're hidden by default
because they carry the auth header.

## Layout

| Path | Purpose |
|------|---------|
| `inventory/hosts.yml` | One inventory group per workspace (all calls run on localhost) |
| `inventory/group_vars/field_eng_east.yml` | CLI profile, warehouse name, list of agents (host and user are looked up at runtime) |
| `agents/*.json` | Agent definitions: the parsed `serialized_space` (version 2) |
| `roles/genie_agent/` | Auth → resolve warehouse → create/update/trash each agent |
| `state/<profile>.json` | Agent title → space_id, written by the playbook (gitignored) |

## Adding an agent

1. Export an existing agent you've built in the UI:
   ```bash
   databricks genie get-space <SPACE_ID> --include-serialized-space -o json \
     --profile field-eng-east | jq '.serialized_space | fromjson' > agents/my_agent.json
   ```
2. Add an entry under `genie_agents` in the group_vars file (`title`, `description`, `file`).
3. Run the deploy playbook.

JSON rules the API enforces: ids are 32-char lowercase hex, text fields are
arrays of strings, and id-keyed arrays are sorted by id. Setting
`enable_entity_matching` also requires `enable_format_assistance`, because the
API adds it automatically (leaving it out shows up as perpetual drift).

## How it works / Ansible concepts used

- **Auth**: `databricks auth token <profile>` mints a short-lived OAuth token
  from the CLI's cached login. It's kept as a fact with `no_log: true` and never
  written to disk.
- **`module_defaults`** injects the bearer header and timeouts into every `uri`
  call, so individual tasks only state the URL, method and body.
- **Idempotency**: the space_id is tracked in `state/<profile>.json`. Each run
  GETs the current space, compares it to the desired JSON (plus title,
  description and warehouse), and only PATCHes on a real difference, so
  `changed` means something. The API returns benchmark questions in its own
  order, so those are sorted by id before comparing. If the space was trashed
  out of band (GET → 404), it's recreated.
- **Check mode**: `uri` doesn't support `--check`, so reads use `check_mode: false`
  and mutations are replaced by "would create/update/trash" tasks marked changed.
- **Environments**: add another inventory group + group_vars file per workspace.
  `genie_catalog_remap` rewrites catalog names in the JSON (e.g. `dev.` → `prod.`)
  when catalogs differ between workspaces.

## API calls

| Action | Call |
|--------|------|
| Resolve warehouse | `GET /api/2.0/sql/warehouses` |
| Ensure folder | `POST /api/2.0/workspace/mkdirs` |
| Read | `GET /api/2.0/genie/spaces/{id}?include_serialized_space=true` |
| Create | `POST /api/2.0/genie/spaces` (`serialized_space` is a JSON **string**) |
| Update | `PATCH /api/2.0/genie/spaces/{id}` |
| Trash | `DELETE /api/2.0/genie/spaces/{id}` |

## Smoke test

```bash
databricks genie start-conversation <SPACE_ID> "What was total net revenue by year?" \
  --profile field-eng-east -o json
```

## Ideas for next steps

- Grant access with `PUT /api/2.0/permissions/genie/{id}`
- A second workspace/environment using `genie_catalog_remap`
- Service-principal (M2M) auth for running from CI
- Package the tasks as a custom module with `state: present/absent`
