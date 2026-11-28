.PHONY: test demo bench

test:
	python3 -m unittest discover -s tests -v
	node --test plugins/pi/*.test.mjs scripts/*.test.mjs
	npm run demo

demo:
	npm run demo

bench:
	node scripts/benchmark.mjs
