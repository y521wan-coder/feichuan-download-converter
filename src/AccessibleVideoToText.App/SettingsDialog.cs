namespace AccessibleVideoToText.App;

public sealed class SettingsDialog : Form
{
    private readonly ComboBox bitrateCombo = new();

    public SettingsDialog(int currentBitrateKbps, bool cloudCredentialsConfigured)
    {
        Text = "设置";
        AccessibleName = "设置";
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
            ColumnCount = 2,
            RowCount = 4
        };
        var bitrateLabel = new Label
        {
            Text = "最终 MP3 码率：",
            AutoSize = true,
            Anchor = AnchorStyles.Left
        };
        bitrateCombo.DropDownStyle = ComboBoxStyle.DropDownList;
        bitrateCombo.AccessibleName = "最终 MP3 码率";
        bitrateCombo.Items.AddRange(["128 kbps", "192 kbps（默认）", "256 kbps", "320 kbps"]);
        bitrateCombo.SelectedIndex = currentBitrateKbps switch
        {
            128 => 0,
            256 => 2,
            320 => 3,
            _ => 1
        };
        layout.Controls.Add(bitrateLabel, 0, 0);
        layout.Controls.Add(bitrateCombo, 1, 0);

        var cloudStatus = new Label
        {
            AutoSize = true,
            MaximumSize = new Size(560, 0),
            Text = cloudCredentialsConfigured
                ? "腾讯云凭据：已使用 Windows DPAPI 为当前用户加密保存。软件不会显示或记录已保存密钥。"
                : "腾讯云凭据：尚未配置。只能填写撤销旧密钥后新建的最小权限 CAM 子账号凭据。",
            AccessibleName = "腾讯云设置状态",
            Margin = new Padding(0, 12, 0, 12)
        };
        layout.SetColumnSpan(cloudStatus, 2);
        layout.Controls.Add(cloudStatus, 0, 1);

        var dataButtons = new FlowLayoutPanel
        {
            AutoSize = true,
            FlowDirection = FlowDirection.LeftToRight,
            Dock = DockStyle.Fill
        };
        var configureCloud = new Button { Text = "配置腾讯云凭据", AutoSize = true };
        configureCloud.Click += (_, _) => Commit(SettingsDialogAction.ConfigureCloud);
        var clearData = new Button { Text = "清除本机全部数据", AutoSize = true };
        clearData.Click += (_, _) => Commit(SettingsDialogAction.ClearLocalData);
        dataButtons.Controls.AddRange([configureCloud, clearData]);
        layout.SetColumnSpan(dataButtons, 2);
        layout.Controls.Add(dataButtons, 0, 2);

        var buttons = new FlowLayoutPanel
        {
            AutoSize = true,
            FlowDirection = FlowDirection.RightToLeft,
            Dock = DockStyle.Fill
        };
        var cancel = new Button
        {
            Text = "取消",
            AutoSize = true,
            DialogResult = DialogResult.Cancel
        };
        var save = new Button
        {
            Text = "保存",
            AutoSize = true,
            DialogResult = DialogResult.None
        };
        save.Click += (_, _) => Commit(SettingsDialogAction.Save);
        buttons.Controls.Add(cancel);
        buttons.Controls.Add(save);
        layout.SetColumnSpan(buttons, 2);
        layout.Controls.Add(buttons, 0, 3);
        Controls.Add(layout);

        AcceptButton = save;
        CancelButton = cancel;
        Shown += (_, _) => bitrateCombo.Focus();
    }

    public int Mp3BitrateKbps => bitrateCombo.SelectedIndex switch
    {
        0 => 128,
        2 => 256,
        3 => 320,
        _ => 192
    };

    public SettingsDialogAction Action { get; private set; }

    private void Commit(SettingsDialogAction action)
    {
        Action = action;
        DialogResult = DialogResult.OK;
        Close();
    }
}

public enum SettingsDialogAction
{
    Save,
    ConfigureCloud,
    ClearLocalData
}
