"""Pause admission atomically, then wait boundedly for tracked jobs to finish."""
import argparse
import sqlite3
import time
from pathlib import Path


def drain(path: Path, timeout: float=30) -> bool:
    if not path.exists():
        # Bootstrap version has no operational journal; the operator must use
        # its existing activity checks. Do not create a misleading empty DB.
        return True
    marker=path.with_suffix('.drain')
    conn=sqlite3.connect(path,timeout=5)
    try:
        conn.execute('BEGIN IMMEDIATE')
        marker.write_text(str(time.time()+timeout+60))
        marker.chmod(0o644)
        conn.commit()
        deadline=time.monotonic()+timeout
        while True:
            active=conn.execute("SELECT count(*) FROM claims WHERE state='running'").fetchone()[0]
            if active==0:
                return True
            if time.monotonic()>=deadline:
                marker.unlink(missing_ok=True)
                return False
            time.sleep(min(.5,max(0,deadline-time.monotonic())))
    finally:
        conn.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('database',type=Path)
    parser.add_argument('--timeout',type=float,default=30)
    args=parser.parse_args()
    raise SystemExit(0 if drain(args.database,args.timeout) else 75)
