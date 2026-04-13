"""
Generate benchmark dataset from live Memgraph.
Queries only mempalace symbols so competitors (who only indexed mempalace) get a fair shot.
"""
import json
import random
from neo4j import GraphDatabase

def generate_cases():
    print("Connecting to Memgraph to sample real CodeNodes...")
    driver = GraphDatabase.driver("bolt://localhost:7687", auth=("", ""))

    with driver.session() as session:
        # Only pull symbols from mempalace so every competitor has fair coverage
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

    # Deduplicate by (name, file) to avoid testing the same symbol twice
    seen = set()
    unique_nodes = []
    for n in nodes:
        key = (n["name"], n["file"])
        if key not in seen:
            seen.add(key)
            unique_nodes.append(n)

    random.shuffle(unique_nodes)
    target_nodes = unique_nodes[:1000]

    templates = [
        "Where is the '{symbol}' {kind} defined?",
        "Which file contains the implementation for '{symbol}'?",
        "Find the definition of the {kind} named '{symbol}'.",
        "Could you point me to '{symbol}' in the codebase?",
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

    out = "datasets/real_code_dataset.json"
    with open(out, "w") as f:
        json.dump(dataset, f, indent=2)
    print(f"Wrote {len(dataset)} queries to {out}")

if __name__ == "__main__":
    generate_cases()
