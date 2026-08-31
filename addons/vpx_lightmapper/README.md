# VPX Light Mapper FINAL 1.0

## HDR Auto / Custom
**HDR Auto** (default ON) uses the original GitHub automatic per-bake HDR range calculation.

When **HDR Auto** is disabled, **Custom HDR Range** becomes available and accepts **0.1–100**.

The custom value is applied at the nesting/render-scaling stage only. Mesh pruning, mesh size and UV generation are not driven by the custom HDR value.

## Included
- Stable mesh/UV/nesting
- LDR/HDR separation
- Memory safety
- Checkpoint/resume
- Clear checkpoint
- Complete nestmap export
- Diagnostics/crash logging
