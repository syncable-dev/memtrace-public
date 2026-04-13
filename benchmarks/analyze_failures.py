import json
from neo4j import GraphDatabase
from collections import Counter


def analyze_failures(json_path: str):
    with open(json_path, 'r') as f:
        failures = json.load(f)
        
    print(f"Analyzing {len(failures)} failed queries against Memgraph...")
    driver = GraphDatabase.driver("bolt://localhost:7687", auth=("", ""))
    
    collision_counts = []
    kind_distribution = Counter()
    unresolved = 0
    
    with driver.session() as session:
        for fail in failures:
            target = fail["target_symbol"]
            
            # See how many times this symbol mathematically exists in the graph to test for ambiguity
            result = session.run("""
            MATCH (s:CodeNode)
            WHERE s.name = $target
            RETURN s.kind AS kind, s.file_path AS file
            """, target=target)
            
            records = list(result)
            
            if not records:
                unresolved += 1
            else:
                collision_counts.append(len(records))
                for r in records:
                    if r["kind"]:
                        kind_distribution[r["kind"]] += 1

    driver.close()
    
    # Calculate stats
    collisions = sum(1 for c in collision_counts if c > 1)
    single_matches_but_failed = sum(1 for c in collision_counts if c == 1)
    
    avg_copies = sum(collision_counts) / len(collision_counts) if collision_counts else 0
    
    print("\n" + "="*50)
    print(" FAILURE ANALYSIS REPORT for GRAPH ROUTING")
    print("="*50)
    print(f"Total Failures: {len(failures)}")
    print(f"Unresolved (Not found exactly by label name): {unresolved}")
    print(f"Colliding Symbols (>1 file shares the same name): {collisions} failures (e.g., 'main', '__init__')")
    print(f"Average duplicates of failed symbols: {avg_copies:.2f} copies per symbol.")
    print(f"Isolated Misses (Only 1 in graph, but didn't match file): {single_matches_but_failed}")
    
    print("\n--- Failure Distribution by Node Kind ---")
    for kind, count in kind_distribution.most_common():
        print(f" - {kind:<12}: {count} occurrences")
        
    print("\nRECOMMENDED ROUTING IMPROVEMENTS:")
    if collisions > len(failures) * 0.5:
        print(" -> Massive proportion of failures are due to namespace collisions.")
        print(" -> FIX: Update MCP 'find_code' tool to require/accept directory constraints to filter multi-file collisions.")
    print(" -> FIX: Introduce full-text indexing (instead of CONTAINS) to resolve hyphenated/camelCase edge cases faster.")
    print("="*50)

if __name__ == "__main__":
    analyze_failures("failed_queries.json")
