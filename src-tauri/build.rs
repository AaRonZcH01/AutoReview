fn main() {
    // HTTP loopback content is a remote origin in Tauri. Custom app commands
    // need an application manifest as well as an explicit remote capability.
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "restart_backend", "backend_status", "exit_app", "hide_to_tray",
        ]),
    )).expect("AutoReview desktop permission manifest could not be generated");
    // Tauri's resource linkage covers the main binary, not Cargo examples.
    // The native camera harness also needs Common Controls v6 (TaskDialogIndirect).
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("windows") && std::env::var("CARGO_CFG_TARGET_ENV").as_deref() == Ok("msvc") {
        // rustc links executables with /MANIFEST:NO; reuse the compiled native
        // resource (which includes the manifest and icon), not linker directives.
        let resource=std::path::PathBuf::from(std::env::var("OUT_DIR").unwrap()).join("resource.lib");
        assert!(resource.is_file(),
            "AutoReview native resource is missing: {}. Refusing to build a camera harness without the Common Controls v6 manifest.",
            resource.display());
        println!("cargo:rustc-link-arg-examples={}",resource.display());
    }
}
