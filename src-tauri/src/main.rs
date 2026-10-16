#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
use std::{
    fs, io::Write, net::TcpStream, path::PathBuf, process::{Child, Command},
    sync::{Mutex, TryLockError, atomic::{AtomicBool, Ordering}},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tauri::{Manager, WebviewUrl, WebviewWindowBuilder, menu::{Menu, MenuItem}, tray::{TrayIconBuilder, TrayIconEvent, MouseButton, MouseButtonState}};
#[cfg(windows)] use std::os::windows::process::CommandExt;

struct Backend {
    service_handle: Mutex<Option<isize>>,
    child: Mutex<Option<Child>>,
    connection: Mutex<Option<serde_json::Value>>,
    gate: Mutex<()>,
    ready: PathBuf,
    session: PathBuf,
    runtime: PathBuf,
    attempts: Mutex<u32>,
    phase: Mutex<String>,
    closing: AtomicBool,
    exit_prompt: AtomicBool,
}
#[cfg(windows)] #[link(name="kernel32")] extern "system" {
    fn OpenProcess(access:u32, inherit:i32, pid:u32)->isize;
    fn TerminateProcess(h:isize, code:u32)->i32;
    fn WaitForSingleObject(h:isize, timeout:u32)->u32;
    fn CloseHandle(h:isize)->i32;
}
fn process_handle(pid:u32)->Option<isize> {
    #[cfg(windows)] unsafe { let h=OpenProcess(0x100001,0,pid); if h!=0 {return Some(h);} }
    None
}
fn release_handle(h:Option<isize>, kill:bool) {
    #[cfg(windows)] if let Some(h)=h {unsafe {
        if kill {TerminateProcess(h,1); WaitForSingleObject(h,1000);}
        CloseHandle(h);
    }}
}
fn service_alive(state:&Backend)->bool {
    #[cfg(windows)] if let Some(h)=*state.service_handle.lock().unwrap() {
        return unsafe {WaitForSingleObject(h,0)==258};
    }
    state.child.lock().unwrap().as_mut().map(|c|matches!(c.try_wait(),Ok(None))).unwrap_or(false)
}
fn force_stop_owned(state:&Backend) {
    release_handle(state.service_handle.lock().unwrap().take(),true);
    if let Some(mut child)=state.child.lock().unwrap().take() {let _=child.kill(); let _=child.wait();}
}
impl Drop for Backend {
    fn drop(&mut self) {
        if let Ok(h)=self.service_handle.get_mut() {release_handle(h.take(),true);}
        if let Ok(child)=self.child.get_mut() {if let Some(mut p)=child.take() {let _=p.kill();let _=p.wait();}}
        let _=fs::remove_file(&self.ready); let _=fs::remove_file(&self.session);
    }
}
fn log_event(message:&str) {
    let base=std::env::var_os("AUTOREVIEW_HOME").map(PathBuf::from).unwrap_or_else(||
        PathBuf::from(std::env::var_os("LOCALAPPDATA").unwrap_or_default()).join("AutoReviewDesktop"));
    let folder=base.join("startup-logs"); let _=fs::create_dir_all(&folder);
    if let Ok(mut f)=fs::OpenOptions::new().create(true).append(true).open(folder.join("desktop.log")) {
        let time=SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs();
        let _=writeln!(f,"{time} pid={} {message}",std::process::id());
    }
}
fn phase(app:&tauri::AppHandle,text:&str) {
    *app.state::<Backend>().phase.lock().unwrap()=text.into(); log_event(text);
}
fn native_dialog(text:&str, confirm:bool)->bool {
    #[cfg(windows)] unsafe {
        #[link(name="user32")] extern "system" {fn MessageBoxW(hwnd:isize,text:*const u16,caption:*const u16,flags:u32)->i32;}
        let t:Vec<u16>=text.encode_utf16().chain(Some(0)).collect();
        let c:Vec<u16>="AutoReview".encode_utf16().chain(Some(0)).collect();
        // Set foreground, with Cancel as the safe default for exit confirmation.
        let flags=if confirm {0x10000|0x20|0x100|0x1} else {0x10000|0x40};
        return MessageBoxW(0,t.as_ptr(),c.as_ptr(),flags)==1;
    }
    #[cfg(not(windows))] {eprintln!("{text}"); true}
}
fn message(text:&str) {native_dialog(text,false);}
#[cfg(windows)] fn single_instance()->Option<isize> {unsafe {
    #[link(name="kernel32")] extern "system" {fn CreateMutexW(a:isize,owned:i32,name:*const u16)->isize;fn GetLastError()->u32;}
    use std::hash::{Hash,Hasher};
    let mut hash=std::collections::hash_map::DefaultHasher::new();
    std::env::var_os("AUTOREVIEW_HOME").hash(&mut hash);
    let name:Vec<u16>=format!("Local\\AutoReviewDesktopMain-{:x}",hash.finish()).encode_utf16().chain(Some(0)).collect();
    let h=CreateMutexW(0,0,name.as_ptr());
    if GetLastError()==183 {if h!=0 {CloseHandle(h);}message("程序正在启动或已在运行，请勿重复运行！");None}
    else if h==0 {message("无法建立程序启动锁。");None} else {Some(h)}
}}
fn progress(app:&tauri::AppHandle,n:u32,label:&str) {
    if let Some(w)=app.get_webview_window("main") {
        let label=serde_json::to_string(label).unwrap();
        let _=w.eval(&format!("if(document.getElementById('progress')){{document.getElementById('progress').value={n};document.getElementById('phase').textContent={label};}}"));
    }
}
fn allowed_origin(url:&tauri::Url,port:Option<u16>)->bool {
    (url.scheme()=="tauri" && url.host_str()==Some("localhost")) ||
    (matches!(url.scheme(),"http"|"https") && url.host_str()==Some("tauri.localhost")) ||
    (url.scheme()=="http" && url.host_str()==Some("127.0.0.1") && port.is_some() && url.port()==port)
}
fn trusted_window(webview:&tauri::WebviewWindow,state:&Backend)->Result<(),String> {
    let port=state.connection.lock().unwrap().as_ref().and_then(|v|v["port"].as_u64()).map(|p|p as u16);
    if webview.label()=="main" && webview.url().map(|u|allowed_origin(&u,port)).unwrap_or(false) {Ok(())}
    else {Err("此窗口不属于当前本地服务，已拒绝桌面操作".into())}
}
fn start_backend(app:&tauri::AppHandle,recovery:bool)->Result<serde_json::Value,String> {
    let result=start_backend_inner(app,recovery);
    if let Err(e)=&result {phase(app,&format!("本地服务未就绪：{e}")); stop_backend(app);}
    result
}
fn start_backend_inner(app:&tauri::AppHandle,recovery:bool)->Result<serde_json::Value,String> {
    let state=app.state::<Backend>();
    if state.closing.load(Ordering::SeqCst) {return Err("程序正在退出".into());}
    let _=fs::remove_file(&state.ready); phase(app,"正在启动本地服务…"); progress(app,25,"正在启动本地服务…");
    let mut cmd=if let Some(root)=std::env::var_os("AUTOREVIEW_SOURCE_ROOT") {
        let root=PathBuf::from(root); let mut c=Command::new(root.join(".venv/Scripts/pythonw.exe"));
        c.current_dir(&root).env("PYTHONPATH",root.join("src/backend")).arg("-m").arg("autoreview.v2.launch"); c
    } else {Command::new(state.runtime.join("AutoReview-Service.exe"))};
    cmd.arg("--no-browser").arg("--ready-file").arg(&state.ready).arg("--parent-pid").arg(std::process::id().to_string()).arg("--connection-file").arg(&state.session);
    if recovery {cmd.arg("--resume-paused");}
    #[cfg(windows)] cmd.creation_flags(0x08000000);
    *state.child.lock().unwrap()=Some(cmd.spawn().map_err(|e|e.to_string())?);
    let begin=Instant::now(); let mut step=0;
    while !state.ready.is_file() {
        if state.closing.load(Ordering::SeqCst) {return Err("程序正在退出".into());}
        let exited=state.child.lock().unwrap().as_mut().map(|p|matches!(p.try_wait(),Ok(Some(_)))).unwrap_or(true);
        if exited {return Err("本地服务启动失败，请查看启动日志与资料库占用情况".into());}
        if begin.elapsed()>Duration::from_secs(50) {return Err("本地服务启动超时，请检查日志".into());}
        let next=begin.elapsed().as_secs();
        if next!=step {progress(app,if next<3 {45} else {65},"正在加载资料库并核对任务状态…");step=next;}
        std::thread::sleep(Duration::from_millis(100));
    }
    let value:serde_json::Value=serde_json::from_slice(&fs::read(&state.ready).map_err(|e|e.to_string())?).map_err(|e|e.to_string())?;
    let port=value["port"].as_u64().filter(|p|*p>0 && *p<=65535).ok_or("本地连接端口不完整")? as u16;
    let url=value["url"].as_str().ok_or("本地连接地址不完整")?.parse::<tauri::Url>().map_err(|e|e.to_string())?;
    if url.scheme()!="http" || url.host_str()!=Some("127.0.0.1") || url.port()!=Some(port) || !url.fragment().unwrap_or("").starts_with("session=") {return Err("本地连接记录不匹配".into());}
    let handle=value["pid"].as_u64().filter(|p|*p<=u32::MAX as u64).and_then(|p|process_handle(p as u32));
    #[cfg(windows)] if handle.is_none() {return Err("无法监护本地服务进程，启动已停止".into());}
    let previous=std::mem::replace(&mut *state.service_handle.lock().unwrap(),handle);
    release_handle(previous,false);
    fs::write(&state.session,serde_json::to_vec(&value).unwrap()).map_err(|e|e.to_string())?;
    *state.connection.lock().unwrap()=Some(value.clone());
    let _=fs::remove_file(&state.ready); phase(app,"本地服务已就绪"); Ok(value)
}
fn stop_backend(app:&tauri::AppHandle) {
    let state=app.state::<Backend>(); phase(app,"正在停止本地服务…");
    let connection=state.connection.lock().unwrap().clone();
    if let Some(v)=connection {
        if let (Some(port),Some(address))=(v["port"].as_u64(),v["url"].as_str()) {
            if let Ok(url)=address.parse::<tauri::Url>() {
                if let Some(nonce)=url.fragment().and_then(|s|s.strip_prefix("session=")) {
                    if let Ok(mut stream)=TcpStream::connect_timeout(&format!("127.0.0.1:{port}").parse().unwrap(),Duration::from_millis(500)) {
                        let _=stream.set_write_timeout(Some(Duration::from_millis(500)));
                        let _=stream.write_all(format!("POST /api/shutdown HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-AutoReview-Session: {nonce}\r\nContent-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{{}}").as_bytes());
                    }
                }
            }
        }
    }
    let started=Instant::now();
    while service_alive(&state) && started.elapsed()<Duration::from_secs(4) {std::thread::sleep(Duration::from_millis(50));}
    force_stop_owned(&state); let _=fs::remove_file(&state.ready); phase(app,"本地服务已停止");
}
#[tauri::command]
async fn restart_backend(app:tauri::AppHandle,webview:tauri::WebviewWindow,force:bool)->Result<serde_json::Value,String> {
    trusted_window(&webview,&app.state::<Backend>())?;
    tauri::async_runtime::spawn_blocking(move|| {
        let state=app.state::<Backend>();
        let _guard=match state.gate.try_lock() {
            Ok(g)=>g,
            Err(TryLockError::WouldBlock)=>return Ok(serde_json::json!({"restarted":false,"in_progress":true,"message":"本地服务正在恢复，请稍候…"})),
            Err(_)=>return Err("恢复状态异常，请退出后重启".into()),
        };
        if state.closing.load(Ordering::SeqCst) {return Err("程序正在退出".into());}
        let alive=service_alive(&state);
        if alive && !force {return Ok(serde_json::json!({"restarted":false,"message":"服务进程仍在运行，正在等待恢复；可手动重启"}));}
        {
            let mut n=state.attempts.lock().unwrap(); if force {*n=0;}
            if *n>=3 {return Err("自动恢复已尝试三次，请检查日志或手动重启".into());} *n+=1;
        }
        // Reap a crashed Python launcher as well as a still-live service. Never
        // replace its only owned handle with a newly spawned process.
        stop_backend(&app);
        for _ in 0..13 {if state.closing.load(Ordering::SeqCst) {return Err("程序正在退出".into());}std::thread::sleep(Duration::from_millis(100));}
        let value=start_backend(&app,true)?;
        Ok(serde_json::json!({"restarted":true,"port":value["port"],"message":"本地服务已重启，正在核对资料库；未完成云端任务已暂停"}))
    }).await.map_err(|e|e.to_string())?
}
#[tauri::command]
fn backend_status(app:tauri::AppHandle,webview:tauri::WebviewWindow)->Result<serde_json::Value,String> {
    let state=app.state::<Backend>(); trusted_window(&webview,&state)?;
    Ok(serde_json::json!({"alive":service_alive(&state),"attempts":*state.attempts.lock().unwrap(),"phase":state.phase.lock().unwrap().clone(),"closing":state.closing.load(Ordering::SeqCst)}))
}
fn begin_exit(app:tauri::AppHandle) {
    if app.state::<Backend>().closing.swap(true,Ordering::SeqCst) {return;}
    phase(&app,"正在退出 AutoReview…");
    if let Some(w)=app.get_webview_window("main") {let _=w.set_title("AutoReview · 正在退出…");}
    // This deadline is native and survives a stalled WebView/IPC or backend.
    let fallback=app.clone();
    std::thread::spawn(move|| {std::thread::sleep(Duration::from_secs(12));log_event("退出达到期限，清理所属后台并结束桌面进程");force_stop_owned(&fallback.state::<Backend>());std::process::exit(0);});
    std::thread::spawn(move|| {
        let state=app.state::<Backend>(); let started=Instant::now();
        loop {
            match state.gate.try_lock() {
                Ok(_guard)=>{stop_backend(&app);break;}
                Err(TryLockError::WouldBlock) if started.elapsed()<Duration::from_secs(2)=>std::thread::sleep(Duration::from_millis(30)),
                _=>{force_stop_owned(&state);break;}
            }
        }
        app.exit(0);
    });
}
#[tauri::command]
fn exit_app(app:tauri::AppHandle,webview:tauri::WebviewWindow)->Result<(),String> {trusted_window(&webview,&app.state::<Backend>())?;begin_exit(app);Ok(())}
fn hide(app:&tauri::AppHandle) {if let Some(w)=app.get_webview_window("main") {let _=w.hide();}}
#[tauri::command]
fn hide_to_tray(app:tauri::AppHandle,webview:tauri::WebviewWindow)->Result<(),String> {trusted_window(&webview,&app.state::<Backend>())?;hide(&app);Ok(())}
fn show(app:&tauri::AppHandle) {if let Some(w)=app.get_webview_window("main") {let _=w.show();let _=w.unminimize();let _=w.set_focus();}}
fn request_exit(app:&tauri::AppHandle) {
    let state=app.state::<Backend>();
    if state.closing.load(Ordering::SeqCst) || state.exit_prompt.swap(true,Ordering::SeqCst) {return;}
    show(app); let app=app.clone();
    std::thread::spawn(move|| {
        let confirmed=native_dialog("退出 AutoReview？\n\n未完成后台任务将停止，已保存的答案、复核结果和历史记录会保留。若有未保存的修改，请取消并先保存。",true);
        if confirmed {begin_exit(app.clone());}
        app.state::<Backend>().exit_prompt.store(false,Ordering::SeqCst);
    });
}
mod camera_permission;
fn main() {
    let instance=std::env::var_os("AUTOREVIEW_SOURCE_ROOT").map(PathBuf::from)
        .unwrap_or_else(||std::env::current_exe().unwrap().parent().unwrap().to_path_buf());
    std::env::set_var("AUTOREVIEW_INSTANCE_DIR",&instance);
    if std::env::var_os("AUTOREVIEW_HOME").is_none() {
        std::env::set_var("AUTOREVIEW_HOME",instance.join(".autoreview-private"));
    }
    #[cfg(windows)] let _instance=match single_instance() {Some(h)=>h,None=>return};
    let result=tauri::Builder::default().invoke_handler(tauri::generate_handler![restart_backend,backend_status,exit_app,hide_to_tray]).setup(|app| {
        let stem=std::env::temp_dir().join(format!("autoreview-{}",std::process::id()));
        app.manage(Backend {service_handle:Mutex::new(None),child:Mutex::new(None),connection:Mutex::new(None),gate:Mutex::new(()),ready:stem.with_extension("ready.json"),session:stem.with_extension("connection.json"),runtime:app.path().resource_dir()?.join("runtime"),attempts:Mutex::new(0),phase:Mutex::new("正在启动…".into()),closing:AtomicBool::new(false),exit_prompt:AtomicBool::new(false)});
        let nav_app=app.handle().clone();
        WebviewWindowBuilder::new(app,"main",WebviewUrl::App("startup.html".into())).data_directory(PathBuf::from(std::env::var_os("AUTOREVIEW_HOME").unwrap()).join("webview")).title("AutoReview · 英语作业批改").inner_size(1380.0,900.0).min_inner_size(950.0,650.0).on_navigation(move|url| {
            let port=nav_app.state::<Backend>().connection.lock().unwrap().as_ref().and_then(|v|v["port"].as_u64()).map(|p|p as u16);
            allowed_origin(url,port)
        }).on_permission_request(|webview,kind| {
            let port=webview.app_handle().state::<Backend>().connection.lock().unwrap().as_ref().and_then(|v|v["port"].as_u64()).map(|p|p as u16);
            match webview.url() {Ok(url)=>camera_permission::response(kind,&url,port,webview.label()=="main"),Err(_)=>tauri::webview::PermissionResponse::Deny}
        }).build()?;
        let show_item=MenuItem::with_id(app,"show","显示工作台",true,None::<&str>)?;
        let hide_item=MenuItem::with_id(app,"hide","收起到托盘（继续运行）",true,None::<&str>)?;
        let quit=MenuItem::with_id(app,"quit","退出 AutoReview…",true,None::<&str>)?;
        let menu=Menu::with_items(app,&[&show_item,&hide_item,&quit])?;
        let mut tray=TrayIconBuilder::new().tooltip("AutoReview · 后台任务继续运行").menu(&menu).show_menu_on_left_click(false)
            .on_menu_event(|app,e|match e.id.as_ref() {"show"=>show(app),"hide"=>hide(app),"quit"=>request_exit(app),_=>{}})
            .on_tray_icon_event(|tray,e| {if matches!(e,TrayIconEvent::Click{button:MouseButton::Left,button_state:MouseButtonState::Up,..}) {show(tray.app_handle());}});
        if let Some(icon)=app.default_window_icon() {tray=tray.icon(icon.clone());} tray.build(app)?;
        let handle=app.handle().clone();
        std::thread::spawn(move|| {
            let state=handle.state::<Backend>(); let _guard=state.gate.lock().unwrap();
            match start_backend(&handle,false) {
                Ok(value)=>{progress(&handle,95,"正在打开教师工作台…");if let Some(w)=handle.get_webview_window("main") {if let Ok(url)=value["url"].as_str().unwrap().parse() {let _=w.navigate(url);}}}
                Err(e)=>progress(&handle,100,&format!("启动失败：{e}。请关闭后重试并查看启动日志。")),
            }
        }); Ok(())
    }).on_window_event(|w,e| {
        if let tauri::WindowEvent::CloseRequested{api,..}=e {
            api.prevent_close();
            if !w.app_handle().state::<Backend>().closing.load(Ordering::SeqCst) {request_exit(w.app_handle());}
        }
    }).run(tauri::generate_context!());
    if let Err(e)=result {message(&format!("无法启动 AutoReview：{e}"));std::process::exit(1);}
}

#[cfg(test)] mod tests {
    use super::*;
    #[test] fn origin_is_owned_port_only() {
        assert!(allowed_origin(&"http://127.0.0.1:55731/".parse().unwrap(),Some(55731)));
        for url in ["http://127.0.0.1:55732/","https://127.0.0.1:55731/","http://localhost:55731/","http://example.com:55731/","http://127.0.0.1.evil.test:55731/"] {
            assert!(!allowed_origin(&url.parse().unwrap(),Some(55731)),"{url}");
        }
        assert!(!allowed_origin(&"http://127.0.0.1:55731/".parse().unwrap(),None));
        assert!(allowed_origin(&"tauri://localhost/startup.html".parse().unwrap(),None));
        assert!(allowed_origin(&"http://tauri.localhost/startup.html".parse().unwrap(),None));
        assert!(!allowed_origin(&"tauri://untrusted/startup.html".parse().unwrap(),None));
    }
}
