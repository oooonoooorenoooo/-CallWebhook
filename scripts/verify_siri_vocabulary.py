"""Check the archived app, so missing resource packaging fails before upload."""
import plistlib
import sys
from pathlib import Path


def verify(app):
    with (app / "Info.plist").open("rb") as source:
        info = plistlib.load(source)
    intents = set(info.get("INIntentsSupported", []))
    if not intents:
        raise ValueError("No supported Siri intents found in the archived app")
    locales = {"Base", "en", "de", info.get("CFBundleDevelopmentRegion", "en")}
    locales.update(info.get("CFBundleLocalizations", []))
    locales.update(p.stem for p in app.glob("*.lproj"))
    for locale in sorted(locales):
        path = app / f"{locale}.lproj" / "AppIntentVocabulary.plist"
        with path.open("rb") as source:
            vocabulary = plistlib.load(source)
        covered = {
            entry.get("IntentName")
            for entry in vocabulary.get("IntentPhrases", [])
            if any(isinstance(phrase, str) and phrase.strip()
                   for phrase in entry.get("IntentExamples", []))
        }
        missing = intents - covered
        if missing:
            raise ValueError(f"{locale}: missing Siri examples for {sorted(missing)}")
    print("Siri vocabulary verified in app bundle: " + ", ".join(sorted(locales)))


if __name__ == "__main__":
    verify(Path(sys.argv[1]))
