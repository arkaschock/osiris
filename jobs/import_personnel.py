"""
Import personnel records from jobs/personnel.csv and backfill publication authors.

The script is intentionally conservative:
- dry-run is the default; pass --apply to write changes
- rows without an email local part are skipped
- duplicate usernames in the CSV are skipped
- department names are mapped only to existing OSIRIS groups
"""

from __future__ import annotations

import argparse
import configparser
import csv
import os
import re
import unicodedata
from datetime import datetime
from pprint import pprint
from typing import Any

from pymongo import MongoClient
from pymongo.errors import PyMongoError


CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.ini")
CSV_PATH = os.path.join(os.path.dirname(__file__), "personnel.csv")


def clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def username_from_email(email: str | None) -> str | None:
    email = clean(email)
    if not email or "@" not in email:
        return None
    username = email.split("@", 1)[0].strip().lower()
    return username or None


def abbreviate_first(first: str | None) -> str:
    if not first:
        return ""
    result: list[str] = []
    for part in re.split(r"(\s+|-|\.)", first):
        if not part or part.isspace() or part == ".":
            continue
        if part == "-":
            result.append("-")
        else:
            result.append(part[0] + ".")
    return "".join(result)


def author_names(last: str, first: str) -> list[str]:
    names = [f"{last}, {first}"]
    first_abbr = abbreviate_first(first)
    if first_abbr:
        names.append(f"{last}, {first_abbr}")
    return list(dict.fromkeys(names))


def norm_name(value: str | None) -> str:
    if not value:
        return ""
    value = value.casefold()
    value = "".join(
        char for char in unicodedata.normalize("NFKD", value)
        if not unicodedata.combining(char)
    )
    value = re.sub(r"[.\s-]+", "", value)
    return value


def read_config() -> configparser.ConfigParser:
    config = configparser.ConfigParser()
    if not config.read(CONFIG_PATH):
        raise RuntimeError(f"Could not read config file: {CONFIG_PATH}")
    return config


def read_personnel() -> list[dict[str, str]]:
    with open(CSV_PATH, newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def load_group_map(db) -> dict[str, str]:
    group_map: dict[str, str] = {}
    for group in db.groups.find({}, {"id": 1, "name": 1, "name_de": 1, "synonyms": 1}):
        group_id = group.get("id")
        if not group_id:
            continue
        for key in ("id", "name", "name_de"):
            value = clean(group.get(key))
            if value:
                group_map[value.casefold()] = group_id
        synonyms = group.get("synonyms") or []
        if isinstance(synonyms, str):
            synonyms = [synonyms]
        for synonym in synonyms:
            synonym = clean(synonym)
            if synonym:
                group_map[synonym.casefold()] = group_id
    return group_map


def slugify(value: str) -> str:
    value = value.replace("ß", "ss").casefold()
    value = "".join(
        char for char in unicodedata.normalize("NFKD", value)
        if not unicodedata.combining(char)
    )
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value or "unit"


def group_unit(department: str) -> str:
    if department.casefold().startswith("abteilung "):
        return "Department"
    return "Unit"


def prepare_group_map(db, rows: list[dict[str, str]], create_groups: bool, apply: bool) -> tuple[dict[str, str], dict[str, Any]]:
    group_map = load_group_map(db)
    departments = sorted(
        {
            dept
            for dept in (clean(row.get("abteilung")) for row in rows)
            if dept and dept.casefold() not in group_map
        },
        key=str.casefold,
    )
    summary: dict[str, Any] = {"created": [], "missing": departments}

    if not create_groups:
        return group_map, summary

    existing_ids = set(group_map.values())
    max_order_group = db.groups.find_one({"parent": "NONE"}, sort=[("order", -1)], projection={"order": 1})
    next_order = int((max_order_group or {}).get("order") or 0) + 1
    today = datetime.now().date().isoformat()

    for department in departments:
        prefix = "dept" if group_unit(department) == "Department" else "unit"
        group_id = f"{prefix}-{slugify(department)}"
        original_group_id = group_id
        suffix = 2
        while group_id in existing_ids or db.groups.find_one({"id": group_id}, {"_id": 1}):
            group_id = f"{original_group_id}-{suffix}"
            suffix += 1

        group_doc = {
            "id": group_id,
            "name": department,
            "name_de": department,
            "unit": group_unit(department),
            "parent": "NONE",
            "level": 1,
            "order": next_order,
            "color": "#000000",
            "inactive": "0",
            "created": today,
            "created_by": "import_personnel.py",
        }
        next_order += 1
        existing_ids.add(group_id)
        group_map[department.casefold()] = group_id
        summary["created"].append(group_doc)
        if apply:
            db.groups.insert_one(group_doc)

    summary["missing"] = []
    return group_map, summary


def build_person(row: dict[str, str], group_map: dict[str, str]) -> tuple[dict[str, Any], str | None]:
    first = clean(row.get("vorname"))
    last = clean(row.get("name"))
    email = clean(row.get("email"))
    username = username_from_email(email)
    if not first or not last or not username:
        raise ValueError("missing first name, last name, or usable email")

    dept = clean(row.get("abteilung"))
    group_id = group_map.get(dept.casefold()) if dept else None
    units = []
    if group_id:
        units.append(
            {
                "id": f"import-{username}-{group_id}",
                "unit": group_id,
                "start": None,
                "end": None,
                "scientific": True,
            }
        )

    person = {
        "username": username,
        "first": first,
        "last": last,
        "displayname": f"{first} {last}",
        "formalname": f"{last}, {first}",
        "first_abbr": abbreviate_first(first),
        "names": author_names(last, first),
        "mail": email,
        "telephone": clean(row.get("phone")),
        "position": clean(row.get("position")),
        "units": units,
        "roles": [],
        "is_active": True,
        "created": datetime.now().date().isoformat(),
        "created_by": "import_personnel.py",
    }
    return person, dept if dept and not group_id else None


def skipped_row(row: dict[str, str], reason: str) -> dict[str, str | None]:
    return {
        "name": clean(row.get("name")),
        "first": clean(row.get("vorname")),
        "email": clean(row.get("email")),
        "reason": reason,
    }


def person_lookup_key(last: str | None, first: str | None) -> tuple[str, str]:
    first = first or ""
    first_token = re.split(r"\s+", first.strip(), maxsplit=1)[0] if first.strip() else ""
    return norm_name(last), norm_name(first_token)


def add_person_to_lookup(person: dict[str, Any], orcid_map, name_map, alias_map) -> None:
    username = person.get("username")
    if not username:
        return
    orcid = clean(person.get("orcid"))
    if orcid:
        orcid_map[orcid.replace("https://orcid.org/", "")] = username
    key = person_lookup_key(person.get("last"), person.get("first"))
    if key[0] and key[1]:
        users = name_map.setdefault(key, [])
        if username not in users:
            users.append(username)
    for alias in person.get("names") or []:
        alias = clean(alias)
        if alias:
            users = alias_map.setdefault(norm_name(alias), [])
            if username not in users:
                users.append(username)


def load_person_lookup(db, extra_persons: list[dict[str, Any]] | None = None) -> tuple[dict[str, str], dict[tuple[str, str], list[str]], dict[str, list[str]]]:
    orcid_map: dict[str, str] = {}
    name_map: dict[tuple[str, str], list[str]] = {}
    alias_map: dict[str, list[str]] = {}

    for person in db.persons.find({}, {"username": 1, "first": 1, "last": 1, "orcid": 1, "names": 1}):
        add_person_to_lookup(person, orcid_map, name_map, alias_map)

    for person in extra_persons or []:
        add_person_to_lookup(person, orcid_map, name_map, alias_map)

    return orcid_map, name_map, alias_map


def match_author(author: dict[str, Any], lookups) -> str | None:
    orcid_map, name_map, alias_map = lookups
    orcid = clean(author.get("orcid"))
    if orcid:
        user = orcid_map.get(orcid.replace("https://orcid.org/", ""))
        if user:
            return user

    last = clean(author.get("last"))
    first = clean(author.get("first"))
    if not last or not first:
        return None

    key = person_lookup_key(last, first)
    users = name_map.get(key, [])
    if len(users) == 1:
        return users[0]

    for alias in (f"{last}, {first}", f"{last}, {abbreviate_first(first)}"):
        users = alias_map.get(norm_name(alias), [])
        if len(users) == 1:
            return users[0]

    return None


def import_persons(db, rows: list[dict[str, str]], apply: bool, group_map: dict[str, str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    usernames = [username_from_email(row.get("email")) for row in rows]
    duplicate_usernames = {u for u in usernames if u and usernames.count(u) > 1}
    staged_persons: list[dict[str, Any]] = []

    summary: dict[str, Any] = {
        "inserted": 0,
        "updated": 0,
        "skipped": [],
        "unmatched_departments": {},
    }

    for row in rows:
        username = username_from_email(row.get("email"))
        if not username:
            summary["skipped"].append(skipped_row(row, "missing usable email"))
            continue
        if username in duplicate_usernames:
            summary["skipped"].append(skipped_row(row, f"duplicate username: {username}"))
            continue

        try:
            person, unmatched_dept = build_person(row, group_map)
        except ValueError as exc:
            summary["skipped"].append(skipped_row(row, str(exc)))
            continue

        staged_persons.append(person)

        if unmatched_dept:
            summary["unmatched_departments"][unmatched_dept] = summary["unmatched_departments"].get(unmatched_dept, 0) + 1

        existing = db.persons.find_one({"username": username}, {"_id": 1})
        if apply:
            if existing:
                update_fields = {
                    key: value
                    for key, value in person.items()
                    if key not in {"created", "created_by", "is_active", "roles", "names", "units"}
                }
                update_fields["updated"] = datetime.now().date().isoformat()
                update_fields["updated_by"] = "import_personnel.py"
                update = {"$set": update_fields}
                add_to_set: dict[str, Any] = {"names": {"$each": person["names"]}}
                if person["units"]:
                    add_to_set["units"] = {"$each": person["units"]}
                update["$addToSet"] = add_to_set
                db.persons.update_one(
                    {"username": username},
                    update,
                )
                summary["updated"] += 1
            else:
                db.persons.insert_one(person)
                summary["inserted"] += 1
        else:
            if existing:
                summary["updated"] += 1
            else:
                summary["inserted"] += 1

    return summary, staged_persons


def backfill_authors(db, apply: bool, extra_persons: list[dict[str, Any]] | None = None) -> dict[str, int]:
    lookups = load_person_lookup(db, extra_persons)
    summary = {"activities_scanned": 0, "activities_updated": 0, "authors_matched": 0}

    cursor = db.activities.find(
        {"authors": {"$elemMatch": {"user": None}}},
        {"authors": 1},
    )
    for activity in cursor:
        summary["activities_scanned"] += 1
        authors = activity.get("authors") or []
        changed = False
        for author in authors:
            if author.get("user") is not None:
                continue
            user = match_author(author, lookups)
            if user:
                author["user"] = user
                changed = True
                summary["authors_matched"] += 1

        if changed:
            summary["activities_updated"] += 1
            if apply:
                db.activities.update_one({"_id": activity["_id"]}, {"$set": {"authors": authors}})

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Import OSIRIS persons from jobs/personnel.csv.")
    parser.add_argument("--apply", action="store_true", help="Write changes to MongoDB. Default is dry-run.")
    parser.add_argument("--create-groups", action="store_true", help="Create missing department/unit groups from the CSV.")
    parser.add_argument("--skip-backfill", action="store_true", help="Only import persons, do not update activities.")
    args = parser.parse_args()

    config = read_config()
    client = MongoClient(config["Database"]["Connection"], serverSelectionTimeoutMS=5000)
    db = client[config["Database"]["Database"]]
    try:
        client.admin.command("ping")
    except PyMongoError as exc:
        print("Could not connect to MongoDB. Start the OSIRIS mongo service and try again.")
        print(f"Mongo error: {exc}")
        return 2

    rows = read_personnel()
    group_map, group_summary = prepare_group_map(db, rows, args.create_groups, args.apply)
    person_summary, staged_persons = import_persons(db, rows, args.apply, group_map)
    print("Mode:", "apply" if args.apply else "dry-run")
    print("Groups:")
    pprint(group_summary)
    print("Personnel import:")
    pprint(person_summary)

    if not args.skip_backfill:
        backfill_summary = backfill_authors(db, args.apply, [] if args.apply else staged_persons)
        print("Author backfill:")
        pprint(backfill_summary)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
