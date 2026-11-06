# Databricks notebook source
# MAGIC %md
# MAGIC # Export a Genie space into this repo
# MAGIC
# MAGIC Pulls a Genie space's definition (`serialized_space`) from the current workspace and writes it to
# MAGIC `agents/<file>.json`, ready to commit and deploy with Ansible.
# MAGIC
# MAGIC Environment-specific values (catalog, schema, ...) are swapped for `{{ placeholders }}` using the
# MAGIC `genie_vars` of the source environment's `inventory/group_vars/<env>.yml`. Ansible renders them back
# MAGIC with each target environment's own `genie_vars` at deploy time.
# MAGIC
# MAGIC **Run it**
# MAGIC - In a workspace **Git folder**: fill in the widgets, run all, then commit from the Git UI.
# MAGIC - **Locally**: `GENIE_SPACE_ID=<id> DATABRICKS_CONFIG_PROFILE=<profile> python notebooks/export_genie_space.py`
# MAGIC - As a **bundle job**: `databricks bundle run export_genie_space --params space_id=<id>,repo_root=<git folder>`

# COMMAND ----------

import json
import os
import re
from pathlib import Path

import yaml
from databricks.sdk import WorkspaceClient


def param(name, default=""):
    """Notebook widget / job parameter on Databricks, GENIE_<NAME> env var locally."""
    try:
        dbutils.widgets.text(name, default)  # noqa: F821
        return dbutils.widgets.get(name).strip()  # noqa: F821
    except Exception:
        return os.environ.get(f"GENIE_{name.upper()}", default).strip()


space_id = param("space_id")
agent_file = param("agent_file")  # relative to repo root; defaults to agents/<title>.json
env = param("env", "field_eng_east")  # inventory group the space was exported from
repo_root = param("repo_root")  # defaults to the repo this notebook lives in

if not space_id:
    raise ValueError("space_id is required")

# COMMAND ----------

# MAGIC %md ## Locate the repo and the source environment's variables

# COMMAND ----------


def find_repo_root():
    for d in [Path.cwd(), *Path.cwd().parents]:
        if (d / "ansible.cfg").exists():
            return d
    raise FileNotFoundError("Couldn't find the repo root (ansible.cfg) above the current directory; set repo_root")


root = Path(repo_root) if repo_root else find_repo_root()
group_vars = yaml.safe_load((root / "inventory" / "group_vars" / f"{env}.yml").read_text()) or {}
genie_vars = group_vars.get("genie_vars") or {}
print(f"Repo root: {root}")
print(f"Variables from {env}: {genie_vars}")

# COMMAND ----------

# MAGIC %md ## Fetch the space

# COMMAND ----------

space = WorkspaceClient().api_client.do(
    "GET", f"/api/2.0/genie/spaces/{space_id}", query={"include_serialized_space": "true"}
)
if not space.get("serialized_space"):
    raise PermissionError("serialized_space is empty: you need CAN EDIT on the space")

definition = json.loads(space["serialized_space"])
print(f"Exported '{space['title']}' ({len(definition.get('data_sources', {}).get('tables', []))} tables)")

# COMMAND ----------

# MAGIC %md ## Normalize and templatize
# MAGIC
# MAGIC - Arrays are sorted by `id` / `identifier` / `column_name` so exports diff cleanly (the API doesn't
# MAGIC   preserve the order of some arrays, e.g. benchmark questions).
# MAGIC - Each `genie_vars` value is replaced by `{{ name }}` wherever it's used as an identifier qualifier,
# MAGIC   i.e. followed by a `.`: `dev_cat.sales.orders` and `` `dev_cat`.`sales` `` both match, while
# MAGIC   `my_dev_cat` or a bare word in prose don't.

# COMMAND ----------

SORT_KEYS = ("id", "identifier", "column_name")
PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def normalize(obj):
    if isinstance(obj, dict):
        return {k: normalize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        items = [normalize(v) for v in obj]
        for key in SORT_KEYS:
            if items and all(isinstance(i, dict) and key in i for i in items):
                return sorted(items, key=lambda i: i[key])
        return items
    return obj


def map_strings(obj, fn):
    if isinstance(obj, dict):
        return {k: map_strings(v, fn) for k, v in obj.items()}
    if isinstance(obj, list):
        return [map_strings(v, fn) for v in obj]
    return fn(obj) if isinstance(obj, str) else obj


# Longest values first so overlapping names (dev vs dev_sales) can't clobber each other
rules = []
for name, value in sorted(genie_vars.items(), key=lambda kv: -len(str(kv[1]))):
    v = re.escape(str(value))
    rules.append((re.compile(rf"`{v}`(?=\.)"), f"`{{{{ {name} }}}}`"))
    rules.append((re.compile(rf"(?<![\w`]){v}(?=\.)"), f"{{{{ {name} }}}}"))


def templatize(s):
    for pattern, replacement in rules:
        s = pattern.sub(replacement, s)
    return s


def render(s):
    return PLACEHOLDER.sub(lambda m: str(genie_vars[m.group(1)]), s)


raw = json.dumps(definition)
if "{{" in raw or "{%" in raw:
    raise ValueError("The space already contains '{{' or '{%', which would clash with Ansible templating")

definition = normalize(definition)
templated = map_strings(definition, templatize)

# Rendering with the source env's values must give back exactly what we exported
assert map_strings(templated, render) == definition, "Templating isn't reversible for this space"

used = sorted(set(PLACEHOLDER.findall(json.dumps(templated))))
print(f"Placeholders used: {used or 'none'}")

# COMMAND ----------

# MAGIC %md ## Write to the repo

# COMMAND ----------

if not agent_file:
    agent_file = "agents/" + re.sub(r"[^a-z0-9]+", "_", space["title"].lower()).strip("_") + ".json"

out = root / agent_file
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(templated, indent=2, ensure_ascii=False) + "\n")
print(f"Wrote {out}")

listed = any(a.get("title") == space["title"] for a in group_vars.get("genie_agents") or [])
if listed:
    print(f"'{space['title']}' is already in genie_agents for {env}.")
else:
    print("Add this to genie_agents in each environment's group_vars to deploy it:\n")
    print(
        yaml.safe_dump(
            [{
                "title": space["title"],
                "description": space.get("description", ""),
                "file": "{{ playbook_dir }}/../" + agent_file,
            }],
            sort_keys=False,
            allow_unicode=True,
        )
    )
