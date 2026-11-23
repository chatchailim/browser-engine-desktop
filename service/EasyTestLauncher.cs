// Normal GUI application launcher. No automation, terminal, or security changes.
using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;
static class EasyTestLauncher {
    [STAThread] static void Main() {
        try {
            string service = AppDomain.CurrentDomain.BaseDirectory;
            string app = Path.GetFullPath(Path.Combine(service, ".."));
            Process.Start(new ProcessStartInfo {
                FileName = Path.Combine(app, ".venv", "Scripts", "pythonw.exe"),
                Arguments = "\"" + Path.Combine(service, "easy_desktop_test.py") + "\"",
                WorkingDirectory = app, UseShellExecute = false
            });
        } catch (Exception e) { MessageBox.Show(e.GetType().Name, "Test launch failed"); }
    }
}
