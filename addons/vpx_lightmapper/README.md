# VPX Light Mapper 3.0.16 — Blender 4.5–5.2

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


## Blender 5.2 compatibility
This release contains the audited Blender 5.0+ GPU/compositor migration, Blender 5.2 GPU backend initialization, Blender 5.x material surface-render-method compatibility, UV editor edge-selection compatibility, and the current OBJ exporter API. See `BLENDER_5.2_COMPATIBILITY_AUDIT.md` for the audit summary.
