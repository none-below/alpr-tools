import re, sys, tomllib, pathlib
readme = pathlib.Path(sys.argv[1]).read_text(); bad = 0
for i, b in enumerate(re.findall(r"```toml\n(.*?)```", readme, re.S), 1):
    try: tomllib.loads(b); print(f"README block {i}: ok")
    except tomllib.TOMLDecodeError as e: bad += 1; print(f"README block {i}: FAIL {e}")
for p in sorted(pathlib.Path(sys.argv[2]).rglob("*.toml")):
    try: tomllib.loads(p.read_text()); print(f"{p}: ok")
    except tomllib.TOMLDecodeError as e: bad += 1; print(f"{p}: FAIL {e}")
sys.exit(1 if bad else 0)
