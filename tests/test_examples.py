import json
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "plugins" / "azure-finops" / "skills"
SKILL_NAMES = (
    "finops-budget-editor",
    "finops-budget-governance",
    "finops-cost-allocation",
    "finops-cost-anomaly-detection",
    "finops-cost-optimization-report",
    "finops-cost-vs-reliability",
    "finops-for-ai",
    "finops-rightsizing-advisor",
)


def _blocks(text, language):
    return re.findall(rf"```{language}\n(.*?)\n```", text, flags=re.DOTALL)


@pytest.mark.parametrize("skill_name", SKILL_NAMES)
def test_skill_example_executes_and_expected_output_is_json(skill_name):
    path = SKILLS / skill_name / "examples" / "README.md"
    text = path.read_text()
    python_blocks = _blocks(text, "python")
    json_blocks = _blocks(text, "json")

    assert len(python_blocks) == 1
    assert len(json_blocks) == 1

    exec(compile(python_blocks[0], str(path), "exec"), {})
    json.loads(json_blocks[0])
