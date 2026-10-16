use tauri::webview::{PermissionKind, PermissionResponse};

// The renderer requests video only in response to the explicit camera controls.
// Never grant access to remote pages, startup pages or other windows. OS privacy
// restrictions still apply; no microphone is used by this product.
pub fn response(kind: PermissionKind, url: &tauri::Url, port: Option<u16>, main_window: bool) -> PermissionResponse {
    match kind {
        PermissionKind::Camera => {
            if main_window && port.is_some() && url.scheme() == "http"
                && url.host_str() == Some("127.0.0.1") && url.port() == port {
                PermissionResponse::Allow
            } else { PermissionResponse::Deny }
        }
        PermissionKind::Microphone => PermissionResponse::Deny,
        _ => PermissionResponse::Default,
    }
}

#[cfg(test)] mod tests {
    use super::*;
    #[test] fn camera_is_owned_window_and_port_only() {
        for (url, port, main, allow) in [
            ("http://127.0.0.1:45678/",Some(45678),true,true),
            ("http://127.0.0.1:45679/",Some(45678),true,false),
            ("http://127.0.0.1:45678/",Some(45678),false,false),
            ("http://127.0.0.1:45678/",None,true,false),
            ("http://example.com:45678/",Some(45678),true,false),
            ("http://127.0.0.1.evil.test:45678/",Some(45678),true,false),
            ("tauri://localhost/",Some(45678),true,false),
        ] {
            assert!(matches!(response(PermissionKind::Camera,&url.parse().unwrap(),port,main),PermissionResponse::Allow)==allow);
        }
        assert!(matches!(response(PermissionKind::Microphone,&"http://127.0.0.1:45678/".parse().unwrap(),Some(45678),true),PermissionResponse::Deny));
    }
}
