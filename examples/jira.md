# Alpha Team - JIRA Reference

## Projects

| Source | Project Key |
| --- | --- |
| Main project | PROJ |
| Strategy board | STRAT |

## Active Work JQL

```
labels = "dashboard-alpha" AND status IN ("In Progress", "Review", "In Review", "Testing") AND component = "Platform Dashboard"
```

## JQL Templates

```
labels = "dashboard-alpha" AND status = "Done" AND resolved >= -14d
```

## Repositories

**upstream** | [platform-hub](https://github.com/your-org/platform-hub)
**midstream** | [platform-dashboard](https://github.com/your-org/platform-dashboard)
