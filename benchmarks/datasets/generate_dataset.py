"""
Generate benchmark dataset from the live ArcadeDB graph.

Queries only mempalace symbols so competitors (who only indexed mempalace)
get a fair shot.  Uses the Neo4j-Bolt plugin wire protocol, so the same
driver that talks to Memgraph also talks to ArcadeDB — only the auth +
database name change.
"""
import json
import os
import random
from neo4j import GraphDatabase

BOLT_URL  = os.environ.get("MEMTRACE_ARCADEDB_BOLT_URL", "bolt://localhost:7687")
USER      = os.environ.get("MEMTRACE_ARCADEDB_USER",     "root")
PASS      = os.environ.get("MEMTRACE_ARCADEDB_PASS",     "playwithdata")
DATABASE  = os.environ.get("MEMTRACE_ARCADEDB_DB",       "memtrace")
OUT       = "datasets/real_code_dataset.json"


def generate_cases():
    print(f"Connecting to ArcadeDB at {BOLT_URL} (db={DATABASE}) to sample CodeNodes...")
    driver = GraphDatabase.driver(BOLT_URL, auth=(USER, PASS))

    with driver.session(database=DATABASE) as session:
        # Only pull symbols from mempalace so every competitor has fair coverage.
        result = session.run("""
        MATCH (s:CodeNode)
        WHERE s.kind IN ['Class', 'Function', 'Method', 'Struct']
          AND s.name IS NOT NULL
          AND s.file_path IS NOT NULL
          AND s.file_path CONTAINS 'mempalace'
          AND NOT s.name IN ['main', '__init__', 'get', 'set', 'run', 'test', 'setup']
          AND size(s.name) > 3
        RETURN s.name AS name, s.file_path AS file, s.kind AS kind
        """)
        nodes = list(result)

    driver.close()
    print(f"Found {len(nodes)} eligible mempalace CodeNodes.")

    # Deduplicate by (name, file) so the same symbol isn't queried twice.
    seen = set()
    unique_nodes = []
    for n in nodes:
        key = (n["name"], n["file"])
        if key not in seen:
            seen.add(key)
            unique_nodes.append(n)

    random.seed(42)  # deterministic sampling for reproducible runs
    random.shuffle(unique_nodes)
    target_nodes = unique_nodes[:1000]

    templates = [
        "Where is the '{symbol}' {kind} defined?",
        "Which file contains the implementation for '{symbol}'?",
        "Find the definition of the {kind} named '{symbol}'.",
        "Could you point me to '{symbol}' in the codebase?",
        "I'm looking for {symbol}",
        "Show me the definition of {symbol}",
    ]

    dataset = []
    for i, node in enumerate(target_nodes):
        tmpl = random.choice(templates)
        query = tmpl.format(symbol=node["name"], kind=node["kind"].lower())
        dataset.append({
            "id": f"q{i+1}",
            "query": query,
            "expected_file": node["file"],
            "target_symbol": node["name"],
            "kind": node["kind"],
        })

    with open(OUT, "w") as f:
        json.dump(dataset, f, indent=2)
    print(f"Wrote {len(dataset)} queries to {OUT}")


if __name__ == "__main__":
    generate_cases()
