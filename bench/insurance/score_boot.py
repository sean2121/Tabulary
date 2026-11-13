#!/usr/bin/env python3
"""Compare the structure of the Ontop bootstrap (Direct Mapping) output with the gold set.

Ontop derives vocabulary names from table and column names, so names cannot be compared.
What is compared is whether the structure was reproduced:
  tables:  if gold has a class, hit when Ontop created a class for the table; fp when gold is none but a class was created.
  columns: if gold has a property, hit when Ontop created a datatype property for the column; fp when gold is none but one was created.
  FKs:     if gold has a relation, hit when Ontop created an object property for the FK; fp when gold is none but one was created.
Direct Mapping mechanically creates everything for every table, column and FK, so precision should be low and recall high.
Tables that gold maps to several classes (role-dependent) can only get one class from Ontop, so they are counted separately as partial.

Usage: python3 score_boot.py boot.owl ../../el_proto/gold_r2rml.yaml
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml
from rdflib import OWL, RDF, Graph

owl_path, gold_path = sys.argv[1], sys.argv[2]
g = Graph().parse(owl_path)
gold = yaml.safe_load(Path(gold_path).read_text())


def local(u: str) -> str:
    return u.rsplit("/", 1)[-1]


classes = {local(str(s)).upper() for s in g.subjects(RDF.type, OWL.Class)}
dtprops = set()
objprops = set()
for s in g.subjects(RDF.type, OWL.DatatypeProperty):
    t, c = local(str(s)).split("#", 1)
    dtprops.add((t.upper(), c.upper()))
for s in g.subjects(RDF.type, OWL.ObjectProperty):
    t, c = local(str(s)).split("#", 1)
    objprops.add((t.upper(), c.upper().removeprefix("REF-")))
print(f"ontop output: {len(classes)} classes, {len(dtprops)} datatype props, {len(objprops)} object props")


def prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return p, r, (2 * p * r / (p + r) if p + r else 0.0)


def report(name, rows):
    tp = fp = fn = tn = partial = 0
    notes = []
    for key, answers, produced in rows:
        want = answers != ["none"] and answers[0] != "none"
        optional = "none" in answers and len(answers) > 1  # none is also acceptable
        if produced and want:
            if len([a for a in answers if a != "none"]) > 1 and name == "tables":
                partial += 1
                notes.append(f"~ {key}: only one class (gold {answers})")
            tp += 1
        elif produced and not want:
            if optional:
                tp += 1
            else:
                fp += 1
                notes.append(f"x {key}: created (gold none)")
        elif not produced and want:
            if optional:
                tn += 1
            else:
                fn += 1
                notes.append(f"x {key}: not created (gold {answers})")
        else:
            tn += 1
    p, r, f = prf(tp, fp, fn)
    print(f"{name:13s} P={p:.3f} R={r:.3f} F1={f:.3f}  (tp={tp} fp={fp} fn={fn} correct-none={tn} partial={partial})")
    for n in notes:
        print("    " + n)


rows = [(t, a, t.upper() in classes) for t, a in gold["tables"].items()]
report("tables", rows)

rows = []
for key, a in gold["columns"].items():
    t, c = key.split(".")
    rows.append((key, a, (t.upper(), c.upper()) in dtprops))
report("columns", rows)

rows = []
for key, a in gold["foreign_keys"].items():
    src, _ = key.split(" -> ")
    t, cols = src.split(".", 1)
    produced = any(ot == t.upper() and all(c.upper() in oc for c in cols.split(",")) for ot, oc in objprops)
    rows.append((key, a, produced))
report("foreign_keys", rows)
