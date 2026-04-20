import json
import argparse
from pathlib import Path
from chromadb_baseline import ChromaBaseline
from memtrace_baseline import MemtraceBaseline

def run_benchmarks(dataset_path: str):
    with open(dataset_path, 'r') as f:
        queries = json.load(f)
        
    target_repo = "/Users/alexthh/Desktop/ZeroToDemo/mempalace"
    traditional = ChromaBaseline(target_dir=target_repo)
    memtrace = MemtraceBaseline()
    
    results = []
    failures = []
    
    print(f"Running REAL benchmarks on {len(queries)} complex architectural queries...\n")
    print("-" * 80)
    print(f"{'Query ID':<10} | {'Method':<15} | {'Tokens Loaded':<15} | {'Time (ms)':<12} | {'Attempts':<10} | {'Acc@1':<5}")
    print("-" * 80)

    try:
        for q in queries:
            # Traditional Run
            t_res = traditional.query(q["query"], q["expected_files"][0], q["target_symbol"])
            print(f"{q['id']:<10} | {'Traditional RAG':<15} | {t_res['tokens_loaded']:<15,d} | {t_res['time_ms']:<12.1f} | {t_res['attempts_to_success']:<10} | {t_res['accuracy_at_1']:<5.1f}")
            
            # Memtrace Run
            m_res = memtrace.query(q["query"], q["expected_files"][0], q["target_symbol"])
            print(f"{q['id']:<10} | {'Memtrace Graph':<15} | {m_res['tokens_loaded']:<15,d} | {m_res['time_ms']:<12.1f} | {m_res['attempts_to_success']:<10} | {m_res['accuracy_at_1']:<5.1f}")
            
            print("-" * 80)
            
            # Track failures for graph algo improvement
            if m_res['accuracy_at_1'] == 0.0:
                failures.append({
                    "id": q["id"],
                    "query": q["query"],
                    "target_symbol": q["target_symbol"],
                    "expected_file": q["expected_files"][0],
                    "tokens_loaded": m_res['tokens_loaded']
                })
            
            results.append({
                "id": q["id"],
                "query": q["query"],
                "complexity": q["complexity"],
                "traditional": t_res,
                "memtrace": m_res
            })
    finally:
        memtrace.close()
        
    output_file = Path("results.jsonl")
    with open(output_file, 'w') as out_f:
        for res in results:
            out_f.write(json.dumps(res) + "\n")
            
    failures_file = Path("failed_queries.json")
    with open(failures_file, 'w') as ff:
        json.dump(failures, ff, indent=2)
            
    print(f"\nSaved raw results to {output_file}")
    print(f"Saved {len(failures)} failed Memtrace queries to {failures_file} for graph routing improvements.")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Memtrace Test Framework Runner")
    parser.add_argument("--dataset", required=True, help="Path to evaluation dataset")
    args = parser.parse_args()
    run_benchmarks(args.dataset)
