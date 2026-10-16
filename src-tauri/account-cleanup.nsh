!macro NSIS_HOOK_PREUNINSTALL
  ${If} $UpdateMode != 1
    nsExec::ExecToStack '"$INSTDIR\runtime\AutoReview-Service.exe" --clear-accounts --instance-root "$INSTDIR"'
    Pop $0
    Pop $1
    ${If} $0 != 0
      MessageBox MB_OK|MB_ICONSTOP "账户凭据清理未完成，卸载已停止。请关闭 AutoReview 后重试，或先在账户面板清除全部账户。"
      Abort
    ${EndIf}
    RMDir /r "$INSTDIR\.autoreview-private"
  ${EndIf}
!macroend
