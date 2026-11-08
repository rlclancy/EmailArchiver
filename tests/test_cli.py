import unittest

from email_archiver.cli import PASSWORD_ENV_VAR, get_password


def _no_prompt(_msg):
    raise AssertionError("should not prompt")


class GetPasswordTests(unittest.TestCase):
    def test_uses_env_var_without_prompting(self):
        pw, src = get_password("u@cox.net", {PASSWORD_ENV_VAR: "abcd efgh"}, _no_prompt)
        self.assertEqual((pw, src), ("abcd efgh", "env"))

    def test_strips_surrounding_whitespace(self):
        pw, _ = get_password("u@cox.net", {PASSWORD_ENV_VAR: "  secret\n"}, _no_prompt)
        self.assertEqual(pw, "secret")

    def test_prompts_when_unset(self):
        pw, src = get_password("u@cox.net", {}, lambda msg: "typed")
        self.assertEqual((pw, src), ("typed", "prompt"))

    def test_prompts_when_empty(self):
        pw, src = get_password("u@cox.net", {PASSWORD_ENV_VAR: "   "}, lambda msg: "typed")
        self.assertEqual((pw, src), ("typed", "prompt"))


if __name__ == "__main__":
    unittest.main()
