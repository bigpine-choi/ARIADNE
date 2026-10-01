"""Assemble an offline Windows x64 distribution from verified vendor artifacts.

The inputs are explicit, hashed files. No data directories or credentials enter
the release. Build metadata records runtime and wheel hashes for later updates.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from zipfile import ZipFile, ZIP_DEFLATED

RUNTIME_HASH='a93abe456ab01bd96d7a085b3cdb6566b3063f4241360d114142fbdb07f0a310'


def build(root,runtime,wheels,hashes,output,profile):
    if hashlib.sha256(runtime.read_bytes()).hexdigest()!=RUNTIME_HASH:raise ValueError('Official Python runtime hash mismatch')
    candidates=sorted(wheels.glob('*.whl'))
    if len(candidates)!=5:raise ValueError('Expected exactly five pinned wheels')
    for path in candidates:
        if path.name not in hashes or hashlib.sha256(path.read_bytes()).hexdigest()!=hashes[path.name]:raise ValueError('Wheel hash mismatch')
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent,prefix='.portable-') as temporary:
        base=Path(temporary)/'ARIADNE';base.mkdir()
        with ZipFile(runtime) as z:z.extractall(base/'runtime')
        (base/'runtime/python314._pth').write_text('python314.zip\n.\n../app\n../packages\n',encoding='ascii')
        packages=base/'packages';packages.mkdir()
        for path in candidates:
            with ZipFile(path) as z:z.extractall(packages)
        app=base/'app';app.mkdir()
        for source in (root/'ariadne').rglob('*'):
            if source.is_file() and '__pycache__' not in source.parts and source.suffix in {'.py','.ps1','.html','.js','.css'}:
                destination=app/source.relative_to(root);destination.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,destination)
        shutil.copytree(root/'browser-extension',base/'browser-extension',ignore=shutil.ignore_patterns('*.pem','*.whx','*.crx','__pycache__'))
        (base/'profile.json').write_text(json.dumps(profile,ensure_ascii=False,indent=2),encoding='utf-8')
        for name,role in [('1-OMEN-시작.cmd','omen'),('2-회사PC-시작.cmd','company-relay')]:
            text='@echo off\r\ncd /d "%~dp0"\r\nstart "" /b "%~dp0runtime\\pythonw.exe" -m ariadne.desktop --profile "%~dp0profile.json" '+role+'\r\n'
            (base/name).write_bytes(text.encode('ascii'))
        shutil.copyfile(root/'docs/HYBRID_OPERATIONS.ko.md',base/'사용안내.md')
        (base/'BUILD-MANIFEST.json').write_text(json.dumps({'python':'3.14.8','runtimeSha256':RUNTIME_HASH,'wheels':hashes},indent=2),encoding='utf-8')
        # A checksum of every included file makes private-file review deterministic.
        files={p.relative_to(base).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(base.rglob('*')) if p.is_file()}
        (base/'FILES.json').write_text(json.dumps(files,ensure_ascii=False,indent=2),encoding='utf-8')
        with ZipFile(output,'w',ZIP_DEFLATED) as z:
            for path in sorted(base.rglob('*')):
                if path.is_file():z.write(path,path.relative_to(base.parent).as_posix())
    return output


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--runtime',type=Path,required=True);parser.add_argument('--wheels',type=Path,required=True)
    parser.add_argument('--hashes',type=Path,required=True);parser.add_argument('--profile',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    path=build(Path(__file__).resolve().parent.parent,args.runtime,args.wheels,json.loads(args.hashes.read_text()),args.output,json.loads(args.profile.read_text(encoding='utf-8-sig')))
    print(json.dumps({'path':str(path.resolve()),'bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}))
