namespace AccessibleVideoToText.App;

public sealed class TrayExitDialog : Form
{
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
            Text = "退出会停止当前本地转换和整个批次，并清理尚未提交的临时输出；源文件不会修改。未来若已有腾讯云任务提交，云任务仍可能继续并占用额度，软件会保存待恢复记录。是否确认退出？",
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
        var continueButton = new Button
        {
            Text = "返回继续",
            AutoSize = true,
            DialogResult = DialogResult.Cancel
        };
        var exitButton = new Button
        {
            Text = "确认退出",
            AutoSize = true,
            DialogResult = DialogResult.OK
        };
        buttons.Controls.Add(continueButton);
        buttons.Controls.Add(exitButton);
        layout.Controls.Add(buttons);
        Controls.Add(layout);

        AcceptButton = continueButton;
        CancelButton = continueButton;
        Shown += (_, _) => continueButton.Focus();
    }
}

