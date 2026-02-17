import pathlib
import sys
import unittest


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


class TestBasic(unittest.TestCase):
    def test_add(self) -> None:
        import {{ cookiecutter.package_name }}

        self.assertEqual({{ cookiecutter.package_name }}.add(2, 3), 5)


if __name__ == "__main__":
    unittest.main()
