namespace AccessibleVideoToText.App;

public enum CancellationScope
{
    Continue,
    SkipCurrent,
    StopBatch
}

public sealed class CancellationScopeDialog : Form
{
    public CancellationScopeDialog(bool cloudTaskAlreadySubmitted)
    {
        Text = "选择取消范围";
        AccessibleName = "选择取消范围";
        StartPosition = FormStartPosition.CenterParent;
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MinimizeBox = false;
        MaximizeBox = false;
        ShowInTaskbar = false;
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
            MaximumSize = new Size(560, 0),
            Text = cloudTaskAlreadySubmitted
                ? "腾讯云任务已经提交，无法取消，仍可能继续并占用额度。您只能停止本地跟踪并保留待恢复记录，或继续处理。"
                : "请选择只跳过当前项目、停止整个批次，或返回继续。源文件永远不会删除。",
            AccessibleName = "取消范围说明"
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
            Text = "继续处理",
            AutoSize = true,
            DialogResult = DialogResult.Cancel
        };
        var stopButton = new Button
        {
            Text = "停止整个批次",
            AutoSize = true
        };
        stopButton.Click += (_, _) => Commit(CancellationScope.StopBatch);
        var skipButton = new Button
        {
            Text = "跳过当前并继续",
            AutoSize = true,
            Enabled = !cloudTaskAlreadySubmitted
        };
        skipButton.Click += (_, _) => Commit(CancellationScope.SkipCurrent);
        buttons.Controls.Add(continueButton);
        buttons.Controls.Add(stopButton);
        buttons.Controls.Add(skipButton);
        layout.Controls.Add(buttons);
        Controls.Add(layout);

        AcceptButton = continueButton;
        CancelButton = continueButton;
        Shown += (_, _) => continueButton.Focus();
    }

    public CancellationScope Choice { get; private set; } = CancellationScope.Continue;

    private void Commit(CancellationScope choice)
    {
        Choice = choice;
        DialogResult = DialogResult.OK;
        Close();
    }
}

