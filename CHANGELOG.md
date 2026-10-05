# CHANGELOG

<!-- version list -->

## v0.1.1 (2026-10-05)

### Bug Fixes

- **deps**: Load maidr.js 4.14.0
  ([`9e50d9c`](https://github.com/xability/maidr-mcp/commit/9e50d9c694672aedc6f05fd4821c64ebea9b22a1))

### Documentation

- Remove pre-release distribution note ([#16](https://github.com/xability/maidr-mcp/pull/16),
  [`3085f1a`](https://github.com/xability/maidr-mcp/commit/3085f1a0ef5f83c4d171d609e65a038de8b22342))


## v0.1.0 (2026-10-02)

### Continuous Integration

- Move the release's state check into a tested script
  ([#14](https://github.com/xability/maidr-mcp/pull/14),
  [`1f950bf`](https://github.com/xability/maidr-mcp/commit/1f950bf7d656cc16a270829c4559ceef26321486))

- Raise the maidr.js pin and py-maidr lock automatically
  ([#11](https://github.com/xability/maidr-mcp/pull/11),
  [`4d0df9b`](https://github.com/xability/maidr-mcp/commit/4d0df9b9c7df373544e0fcbc36a58700aaf31ec8))

- Release to PyPI, GHCR and the MCP Registry with semantic-release
  ([#12](https://github.com/xability/maidr-mcp/pull/12),
  [`bea9764`](https://github.com/xability/maidr-mcp/commit/bea976462ebdb2d63828e45ebd0242af6c50d354))

- Review pull requests with Claude ([#3](https://github.com/xability/maidr-mcp/pull/3),
  [`ed71e7a`](https://github.com/xability/maidr-mcp/commit/ed71e7a82354dec75dcbad42f2b0870f680c0b9a))

- Run the maidr update's write jobs only from main
  ([#15](https://github.com/xability/maidr-mcp/pull/15),
  [`eaee9ff`](https://github.com/xability/maidr-mcp/commit/eaee9ffba353909487743b0c1298119ae03d3091))

### Documentation

- Describe connecting ChatGPT without a public address
  ([#2](https://github.com/xability/maidr-mcp/pull/2),
  [`3215fb0`](https://github.com/xability/maidr-mcp/commit/3215fb08e97b59933b0f8bd74f9a2cce5feb96ef))

### Features

- Add an optional access token for the http transport
  ([#7](https://github.com/xability/maidr-mcp/pull/7),
  [`bd05c57`](https://github.com/xability/maidr-mcp/commit/bd05c57b6304670b264a3e45f2ad778676b80a5d))

- Change a shown chart in place with update_chart
  ([#5](https://github.com/xability/maidr-mcp/pull/5),
  [`ddb57a6`](https://github.com/xability/maidr-mcp/commit/ddb57a618540a6414aeea93ee1a7120bef265751))

- Draw step, violin, pie and candlestick charts and scatter trend lines
  ([#8](https://github.com/xability/maidr-mcp/pull/8),
  [`cdf4d27`](https://github.com/xability/maidr-mcp/commit/cdf4d270805eacb3a8102522b1513bd0f4383380))

- Fall back to short polls when a host cuts the view's long poll
  ([#6](https://github.com/xability/maidr-mcp/pull/6),
  [`9af60d0`](https://github.com/xability/maidr-mcp/commit/9af60d01df3d657032dbedb70da2760622ef06ad))

- Let the model take the reader into the chart when they ask
  ([#10](https://github.com/xability/maidr-mcp/pull/10),
  [`1d76516`](https://github.com/xability/maidr-mcp/commit/1d76516a6f2f235db67579bf57ad029b4a6c53cc))

- Relay maidr's command tools and tell the reader what waits for them
  ([#9](https://github.com/xability/maidr-mcp/pull/9),
  [`7b902d5`](https://github.com/xability/maidr-mcp/commit/7b902d5e77e321ca817846ad0305105372acf9d4))

- Show accessible maidr charts in ChatGPT and Claude, and let the model move the reader
  ([#1](https://github.com/xability/maidr-mcp/pull/1),
  [`7ed7dbe`](https://github.com/xability/maidr-mcp/commit/7ed7dbece7d75eb7eab9bc00712724d3c18a44be))
