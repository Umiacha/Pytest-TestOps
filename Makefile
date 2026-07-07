.PHONY: start-demo-from-root

start-demo-from-root:
	uv run --directory demo-projects/demo-algorithms pytest --testops-json-report=.testops/demo-algorithms.json
