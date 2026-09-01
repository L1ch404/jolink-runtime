import importlib.util
from pathlib import Path


_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "run_swe_polybench_stability.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "run_swe_polybench_stability",
    _SCRIPT,
)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
_build_system = _MODULE._build_system
_classify = _MODULE._classify
_selector = _MODULE._selector


def test_detects_maven_authority_from_official_test_command() -> None:
    assert (
        _build_system(
            "export JAVA_HOME=/jdk; cd /testbed && mvn clean verify -Dtest=X"
        )
        == "maven"
    )


def test_detects_conditional_maven_wrapper_authority() -> None:
    assert (
        _build_system(
            "if [ -e mvnw ]; then ./mvnw test; else mvn test; fi"
        )
        == "maven"
    )


def test_detects_gradle_authority_from_official_test_command() -> None:
    assert _build_system("cd /testbed && ./gradlew test --tests X") == "gradle"


def test_leaves_unknown_authority_unset() -> None:
    assert _build_system("cd /testbed && ./custom-test-runner") is None


def test_selector_prefers_a_direct_java_method_over_report_display_name() -> None:
    selector, source = _selector(
        {
            "P2P": str(
                [
                    "example.Test.parameterized{String}[3]",
                    "example.Test.directMethod",
                ]
            ),
            "F2P": "[]",
        }
    )

    assert selector == "example.Test#directMethod"
    assert source == "P2P"


def test_bootstrap_tls_failure_is_environment_not_product() -> None:
    assert (
        _classify(
            {
                "stage": "bootstrap",
                "error_code": "FAST_TEST_BOOTSTRAP_FAILED",
                "jolink": {
                    "official_selected": {"outcome": "passed"},
                    "stages": {
                        "baseline": {
                            "bootstrap_log_tail": [
                                "Could not transfer artifact: "
                                "Remote host terminated the handshake"
                            ]
                        }
                    },
                },
            }
        )
        == "DATASET_OR_ENVIRONMENT_FAILURE"
    )


def test_gold_dependent_test_patch_is_environment_not_product() -> None:
    assert (
        _classify(
            {
                "stage": "bootstrap",
                "error_code": "FAST_TEST_BOOTSTRAP_FAILED",
                "jolink": {
                    "official_selected": {"outcome": "not_found"},
                    "stages": {
                        "baseline": {
                            "bootstrap_log_tail": ["cannot find symbol"]
                        }
                    },
                },
            }
        )
        == "DATASET_OR_ENVIRONMENT_FAILURE"
    )


def test_unexplained_bootstrap_failure_remains_product_candidate() -> None:
    assert (
        _classify(
            {
                "stage": "bootstrap",
                "error_code": "FAST_TEST_BOOTSTRAP_FAILED",
                "jolink": {
                    "official_selected": {"outcome": "passed"},
                    "stages": {
                        "baseline": {"bootstrap_log_tail": ["internal error"]}
                    },
                },
            }
        )
        == "PRODUCT_BUG"
    )
