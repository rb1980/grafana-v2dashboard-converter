# Grafana schema-v2 downgrade helper

Use `scripts/convert_grafana_dashboard_v2.py` to turn a dashboard exported with the new `dashboard.grafana.app/v2beta1` schema into the older classic dashboard JSON model:

```bash
python3 ./scripts/convert_grafana_dashboard_v2.py /path/to/v2.json -o ./classic-dashboard.json
```

Notes:
- Grid layouts are mapped directly to classic `gridPos`.
- Auto grid layouts are approximated into Grafana's classic 24-column grid.
- Tabs are downgraded into rows because the classic model has no tab container.
- Unsupported schema-v2-only features are reported as warnings on stderr.