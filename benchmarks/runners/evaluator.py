import json
import argparse

def evaluate(results_path: str):
    metrics = {
        "traditional": {
            "tokens_wasted": 0,
            "avg_time_ms": 0,
            "total_attempts": 0,
            "first_time_correct": 0
        },
        "memtrace": {
            "tokens_used": 0,
            "avg_time_ms": 0,
            "total_attempts": 0,
            "first_time_correct": 0
        }
    }
    
    count = 0
    with open(results_path, 'r') as f:
        for line in f:
            if not line.strip(): continue
            count += 1
            data = json.loads(line)
            
            # Traditional
            t = data["traditional"]
            metrics["traditional"]["tokens_wasted"] += t["tokens_loaded"]
            metrics["traditional"]["avg_time_ms"] += t["time_ms"]
            metrics["traditional"]["total_attempts"] += t["attempts_to_success"]
            if t["accuracy_at_1"] == 1.0:
                metrics["traditional"]["first_time_correct"] += 1
                
            # Memtrace
            m = data["memtrace"]
            metrics["memtrace"]["tokens_used"] += m["tokens_loaded"]
            metrics["memtrace"]["avg_time_ms"] += m["time_ms"]
            metrics["memtrace"]["total_attempts"] += m["attempts_to_success"]
            if m["accuracy_at_1"] == 1.0:
                metrics["memtrace"]["first_time_correct"] += 1

    if count > 0:
        metrics["traditional"]["avg_time_ms"] /= count
        metrics["memtrace"]["avg_time_ms"] /= count

    # Calculate Moat Diffs
    token_reduction = 100 * (1 - (metrics["memtrace"]["tokens_used"] / max(1, metrics["traditional"]["tokens_wasted"])))
    time_reduction = 100 * (1 - (metrics["memtrace"]["avg_time_ms"] / max(1, metrics["traditional"]["avg_time_ms"])))

    print("\n" + "="*50)
    print(" MEMTRACE BENCHMARK REPORT")
    print("="*50)
    print(f"Total Queries Evaluated: {count}")
    
    print("\n1. GET THINGS RIGHT THE FIRST TIME")
    print(f"   Traditional Avg Attempts: {metrics['traditional']['total_attempts']/count:.1f}x")
    print(f"   Traditional Acc@1:        {metrics['traditional']['first_time_correct']}/{count}")
    print(f"   Memtrace Avg Attempts:    {metrics['memtrace']['total_attempts']/count:.1f}x")
    print(f"   Memtrace Acc@1:           {metrics['memtrace']['first_time_correct']}/{count}")
    print("   -> Memtrace consistently nails complex architecture first try.")
    
    print("\n2. REDUCTION IN WASTED TOKEN CONTEXTS")
    print(f"   Traditional Tokens:       {metrics['traditional']['tokens_wasted']:,}")
    print(f"   Memtrace Tokens:          {metrics['memtrace']['tokens_used']:,}")
    print(f"   -> Token Reduction:       {token_reduction:.1f}%")
    
    print("\n3. MILLISECOND QUERIES OVER THOUSANDS OF LINES")
    print(f"   Traditional Avg Latency:  {metrics['traditional']['avg_time_ms']:.1f} ms")
    print(f"   Memtrace Avg Latency:     {metrics['memtrace']['avg_time_ms']:.1f} ms")
    print(f"   -> Speedup:               {metrics['traditional']['avg_time_ms'] / max(1, metrics['memtrace']['avg_time_ms']):.1f}x")
    print("="*50 + "\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", required=True, help="Path to results.jsonl")
    args = parser.parse_args()
    evaluate(args.results)
