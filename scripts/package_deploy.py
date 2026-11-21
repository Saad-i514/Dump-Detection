"""Build the private AWS deployment bundle, including the active PyTorch weights."""

from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    model = ROOT / "models/road_damage_best.pt"
    if not model.is_file():
        raise SystemExit("Missing models/road_damage_best.pt")
    destination = ROOT / ".deploy/bundle.tar.gz"
    destination.parent.mkdir(exist_ok=True)
    with tarfile.open(destination, "w:gz") as archive:
        for directory in ("api", "src"):
            for source in sorted((ROOT / directory).rglob("*.py")):
                archive.add(source, arcname=source.relative_to(ROOT))
        for source in (model, ROOT / "requirements-api.txt"):
            archive.add(source, arcname=source.relative_to(ROOT))
    with tarfile.open(destination) as archive:
        names = archive.getnames()
        assert "api/main.py" in names and "models/road_damage_best.pt" in names
        assert all(not name.startswith(("/", ".venv", ".env")) and ".." not in Path(name).parts for name in names)
    print(f"Created {destination} ({destination.stat().st_size / 1024**2:.1f} MiB)")


if __name__ == "__main__":
    main()
