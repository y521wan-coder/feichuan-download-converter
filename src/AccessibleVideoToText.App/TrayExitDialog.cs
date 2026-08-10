namespace AccessibleVideoToText.App;

public enum TaskCloseChoice
{
    ContinueInTray,
    CancelTaskAndExit,
    ReturnToSoftware
}

public sealed class TrayExitDialog : Form
{
    public TaskCloseChoice Choice { get; private set; } = TaskCloseChoice.ReturnToSoftware;

    public TrayExitDialog()
    {
        Text = "任务进行中，确认退出软件";
        AccessibleName = "任务进行中，确认退出软件";
        StartPosition = FormStartPosition.CenterScreen;
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MinimizeBox = false;
        MaximizeBox = false;
        ShowInTaskbar = true;
        AutoSize = true;
        AutoSizeMode = AutoSizeMode.GrowAndShrink;
        AutoScaleMode = AutoScaleMode.Dpi;
        Padding = new Padding(12);

        var layout = new TableLayoutPanel
        {
            AutoSize = true,
            ColumnCount = 1,
            RowCount = 2
        };
        var explanation = new Label
        {
            AutoSize = true,
            MaximumSize = new Size(600, 0),
            Text = "任务仍在进行。可以转入托盘继续；也可以取消任务并退出，软件会清理尚未提交的临时输出且不修改源文件。若腾讯云任务已经提交，云任务仍可能继续并占用额度，软件会保留待恢复记录。",
            AccessibleName = "退出影响说明"
        };
        layout.Controls.Add(explanation);

        var buttons = new FlowLayoutPanel
        {
            AutoSize = true,
            FlowDirection = FlowDirection.RightToLeft,
            Dock = DockStyle.Fill,
            Margin = new Padding(0, 12, 0, 0)
        };
        var returnButton = new Button
        {
            Text = "返回软件",
            AutoSize = true,
            DialogResult = DialogResult.Cancel
        };
        var cancelAndExitButton = new Button
        {
            Text = "取消任务并退出",
            AutoSize = true,
        };
        cancelAndExitButton.Click += (_, _) =>
        {
            Choice = TaskCloseChoice.CancelTaskAndExit;
            DialogResult = DialogResult.OK;
            Close();
        };
        var trayButton = new Button
        {
            Text = "转入托盘继续（默认）",
            AutoSize = true
        };
        trayButton.Click += (_, _) =>
        {
            Choice = TaskCloseChoice.ContinueInTray;
            DialogResult = DialogResult.OK;
            Close();
        };
        buttons.Controls.Add(returnButton);
        buttons.Controls.Add(cancelAndExitButton);
        buttons.Controls.Add(trayButton);
        layout.Controls.Add(buttons);
        Controls.Add(layout);

        AcceptButton = trayButton;
        CancelButton = returnButton;
        Shown += (_, _) => trayButton.Focus();
    }
}
