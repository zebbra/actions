# `enforce-pr-label/v1` - Enforce PR Label

Fail if a pull request lacks one of the required PR labels

## Quick usage
```yaml
jobs:
  example:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: zebbra/actions/enforce-pr-label@enforce-pr-label/v1
      # with:
      #   <input_name>: <value>
```

## Inputs
| Name | Required | Default | Description |
| ---- | -------- | ------- | ----------- |
| `required_labels` | false | `pr-bugfix pr-breaking-change pr-other pr-new-feature pr-security` | Whitespace-separated list of valid PR labels |

## Outputs
(none)

## Technical
- runs.using: `composite`
- action path: [enforce-pr-label/action.yml](https://github.com/zebbra/actions/blob/enforce-pr-label/v1/enforce-pr-label/action.yml)

### Referenced actions
(none)

### Files
- [enforce-pr-label/action.yml](https://github.com/zebbra/actions/blob/enforce-pr-label/v1/enforce-pr-label/action.yml)

## Other versions
(none)
