# Branding sources

The PNG files in `source/` are the approved raster masters for this integration.

- `icon-small-master.png`: compact light AP + Bluetooth variant, 1024×1024.
- `dark-icon-small-master.png`: compact dark variant, 1024×1024.
- `logo-master.png`: horizontal lock-up, 1280×512.
- `readme-banner-master.png`: 1500×500 banner.
- `social-preview-master.png`: 1280×640 GitHub preview.

The station is kept in the large assets, but removed from the small icon.
The Bluetooth symbol visually replaces the B; there is no additional B.

The assets are derived from the already approved conceptual PNGs and not from an
SVG reinterpretation. The full conceptual ZIP is not included to avoid duplicating
large files in the repository history.

## Packaging flow

1. Keep `source/*-master.png` as the canonical raster masters.
2. Export the integration brand assets declared in `manifest.json`:
   - `custom_components/allpowers_ble/brand/icon.png` (256x256)
   - `custom_components/allpowers_ble/brand/icon@2x.png` (512x512)
   - `custom_components/allpowers_ble/brand/dark_icon.png` (256x256)
   - `custom_components/allpowers_ble/brand/dark_icon@2x.png` (512x512)
   - `custom_components/allpowers_ble/brand/logo.png` (640x256)
   - `custom_components/allpowers_ble/brand/logo@2x.png` (1280x512)
3. Export documentation assets declared in `manifest.json`:
   - `docs/assets/branding/readme-banner.png` (1500x500)
   - `docs/assets/branding/social-preview.png` (1280x640)
4. Run `python scripts/validate_repository.py` to verify required files and icon
   dimensions before committing.
5. Keep `manifest.json` aligned with any future filename or resolution change.
