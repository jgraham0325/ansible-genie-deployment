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
| `agents/*.json` | Agent definitions: the parsed `serialized_space` (version 2), with `{{ placeholders }}` |
| `notebooks/export_genie_space.py` | Exports a space from a workspace into `agents/` |
| `databricks.yml` | Dev-only bundle that deploys the export notebook + a job to run it |
| `roles/genie_agent/` | Auth → resolve warehouse → create/update/trash each agent |
| `state/<profile>.json` | Agent title → space_id, written by the playbook (gitignored) |

## Dev → git → Ansible workflow

The intended loop is: build or tune an agent in the **dev** workspace's Genie UI,
export it into the repo, commit, and let Ansible deploy it everywhere.

```
dev workspace ──export notebook──▶ agents/x.json ──git──▶ ansible-playbook ──▶ dev / test / prod
 samples.tpch.orders      {{ catalog }}.{{ schema }}.orders      prod_cat.sales.orders
```

### Variable substitution

Each environment's group_vars has a `genie_vars` map, e.g.

```yaml
# inventory/group_vars/field_eng_east.yml (dev)   # group_vars/prod.yml
genie_vars:                                         genie_vars:
  catalog: samples                                    catalog: prod_cat
  schema: tpch                                        schema: sales
```

- **On export**, the notebook reads `genie_vars` for the source environment
  (`env` parameter) and replaces each value with `{{ name }}` wherever it's used
  as an identifier qualifier, i.e. followed by `.`. That covers table identifiers,
  example SQL, join specs, snippets and benchmarks, in both plain and
  `` `backticked` `` form. Partial matches (`my_samples.`) and plain words in
  prose are left alone. It then renders the result with the dev values and
  asserts it's identical to what it exported, so the substitution is lossless.
- **On deploy**, Ansible loads the file with the `template` lookup and fills in
  the target environment's `genie_vars`.
- **Not in the JSON, so no substitution needed**: the warehouse (resolved by
  name per environment), the space id (tracked in `state/`) and the parent folder
  (built from the current user).
- Placeholders are Jinja, so the notebook refuses to export a space that already
  contains `{{` or `{%`.

Pick values that are distinctive. A dev catalog called `main`, for example,
would also match any other `main.` qualifier in your SQL.

### Running the export

The notebook takes `space_id` (required), `agent_file` (defaults to
`agents/<title>.json`), `env` (default `field_eng_east`) and `repo_root`. It
normalizes array order so exports diff cleanly, and prints the `genie_agents`
entry to add if the agent is new.

- **From a workspace Git folder** (Workspace → Create → Git folder → this repo):
  open `notebooks/export_genie_space`, fill in the widgets, run all, then review
  and commit the change in the Git folder UI.
- **Locally**, writing straight into your clone:
  ```bash
  GENIE_SPACE_ID=<id> GENIE_AGENT_FILE=agents/my_agent.json \
    DATABRICKS_CONFIG_PROFILE=field-eng-east .venv/bin/python notebooks/export_genie_space.py
  ```
- **As a job via the bundle** (dev target only; runs on serverless):
  ```bash
  databricks bundle deploy
  databricks bundle run export_genie_space --params \
    space_id=<id>,agent_file=agents/my_agent.json,repo_root=/Workspace/Users/<you>/ansible-genie-deployment
  ```
  Point `repo_root` at your Git folder. Without it, the job writes into the
  bundle's own synced files, which the next `bundle deploy` overwrites.

The bundle is for local development only. Ansible stays the deployment tool for
the agents themselves; the bundle just gets the notebook into your dev workspace
and gives you a one-command way to run it.

Then add the agent to `genie_agents` in each environment's group_vars (if new)
and run the deploy playbook.

Heads-up: if you edit an Ansible-managed agent in the UI, export it before the
next deploy, or Ansible will overwrite your edits. `--check` shows when an
agent has drifted.

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
- **Environments**: add another inventory group + group_vars file per workspace,
  each with its own `databricks_profile`, `genie_warehouse_name` and `genie_vars`.

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
- A second workspace/environment with its own `genie_vars`
- Service-principal (M2M) auth for running from CI
- Package the tasks as a custom module with `state: present/absent`
