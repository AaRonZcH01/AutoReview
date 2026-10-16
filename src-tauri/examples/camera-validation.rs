// Isolated local camera acceptance harness. Never launched by normal source entry.
#[path="../src/camera_permission.rs"] mod camera_permission;
use tauri::{WebviewUrl,WebviewWindowBuilder};
fn main(){
    let url:tauri::Url=std::env::var("AUTOREVIEW_CAMERA_TEST_URL").expect("explicit local test URL").parse().unwrap();
    assert_eq!(url.scheme(),"http");assert_eq!(url.host_str(),Some("127.0.0.1"));
    let port=url.port().unwrap();
    let storage=std::env::var("AUTOREVIEW_CAMERA_TEST_STORAGE").expect("ephemeral WebView storage");
    tauri::Builder::default().setup(move|app|{
        WebviewWindowBuilder::new(app,"main",WebviewUrl::External(url.clone())).title("AutoReview 相机隔离验收 仅本地模拟")
            .inner_size(1400.,950.).data_directory(std::path::PathBuf::from(storage.clone()))
            .on_navigation(move|u|u.scheme()=="http"&&u.host_str()==Some("127.0.0.1")&&u.port()==Some(port))
            .on_permission_request(move|w,k|camera_permission::response(k,&w.url().unwrap(),Some(port),w.label()=="main"))
            .build()?;Ok(())
    }).run(tauri::generate_context!()).expect("camera test window");
}
