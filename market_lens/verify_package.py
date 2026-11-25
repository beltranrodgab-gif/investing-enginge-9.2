"""Check the extracted distribution against its embedded SHA-256 list (not a digital signature)."""
from pathlib import Path
import hashlib,json,sys
ROOT=Path(__file__).resolve().parent
def main():
 paths=json.loads((ROOT/'file_hashes.json').read_text('utf-8'));bad=[]
 for name,expected in paths.items():
  p=(ROOT/name).resolve()
  if not p.is_relative_to(ROOT) or not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest()!=expected:bad.append(name)
 if bad:raise SystemExit('Missing or changed: '+', '.join(bad))
 print('OK:',len(paths),'distribution files verified')
if __name__=='__main__':main()
