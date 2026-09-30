import json
import pathlib
import re
import sys
import unittest


BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from core.config import RESOURCES_DIR
from infrastructure.model_registry import BUILTIN_MODELS


class ModelRegistryTests(unittest.TestCase):
    def test_frontend_and_backend_builtin_models_match(self):
        frontend_config = (
            BACKEND_ROOT.parent
            / "vtuber-web-app"
            / "src"
            / "live2d"
            / "LAppDefine.ts"
        ).read_text(encoding="utf-8")
        frontend_models = set(
            re.findall(
                r"name:\s*'([^']+)'.*?directory:\s*'([^']+)'.*?fileName:\s*'([^']+)'",
                frontend_config,
                re.DOTALL,
            )
        )
        backend_models = {
            (model["name"], model["directory"], model["fileName"])
            for model in BUILTIN_MODELS
        }

        self.assertEqual(frontend_models, backend_models)

    def test_builtin_model_files_and_references_exist(self):
        resources_dir = pathlib.Path(RESOURCES_DIR)

        for model in BUILTIN_MODELS:
            with self.subTest(model=model["name"]):
                model_json_path = resources_dir / model["directory"] / model["fileName"]
                self.assertTrue(model_json_path.is_file(), model_json_path)

                model_json = json.loads(model_json_path.read_text(encoding="utf-8-sig"))
                references = model_json["FileReferences"]
                referenced_files = [references["Moc"], *references.get("Textures", [])]
                referenced_files.extend(
                    value
                    for key in ("Physics", "Pose", "DisplayInfo", "UserData")
                    if (value := references.get(key))
                )
                referenced_files.extend(
                    expression["File"] for expression in references.get("Expressions", [])
                )
                referenced_files.extend(
                    motion["File"]
                    for motions in references.get("Motions", {}).values()
                    for motion in motions
                )

                for relative_path in referenced_files:
                    self.assertTrue(
                        (model_json_path.parent / relative_path).is_file(),
                        relative_path,
                    )


if __name__ == "__main__":
    unittest.main()
