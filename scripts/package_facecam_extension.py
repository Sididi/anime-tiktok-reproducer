"""Build the small standalone Windows editor extension distribution."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

root = Path(__file__).resolve().parents[1]
source = root / 'premiere-extension'
output = source / 'dist' / 'Facecam-Import-1.0.0.zip'
output.parent.mkdir(exist_ok=True)
with ZipFile(output, 'w', ZIP_DEFLATED) as archive:
    archive.write(source / 'install_facecam_extension.bat', 'install_facecam_extension.bat')
    for path in sorted((source / 'facecam-import').rglob('*')):
        if path.is_file():
            archive.write(path, path.relative_to(source).as_posix())
print(output)
