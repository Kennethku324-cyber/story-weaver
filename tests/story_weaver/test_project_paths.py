from pathlib import Path


def test_package_paths_point_to_the_checked_out_generative_agents_root():
    from story_weaver.paths import DATA_ROOT, GEN_ROOT

    expected_root = Path(__file__).resolve().parents[2] / "generative_agents"
    assert GEN_ROOT == expected_root
    assert DATA_ROOT == expected_root / "data"
    assert (DATA_ROOT / "prompts_gm" / "gm_round_summary.txt").is_file()
