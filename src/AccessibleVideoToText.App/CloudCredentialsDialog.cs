using AccessibleVideoToText.Core;

namespace AccessibleVideoToText.App;

public sealed class CloudCredentialsDialog : Form
{
    private readonly TextBox appIdBox = new();
    private readonly TextBox secretIdBox = new();
    private readonly TextBox secretKeyBox = new();
    private readonly CheckBox acknowledgeBox = new();
    private readonly Button saveButton = new();

    public CloudCredentialsDialog()
    {
        Text = "首次配置腾讯云";
        AccessibleName = "首次配置腾讯云";
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
            RowCount = 7
        };
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 430));

        var notice = new Label
        {
            AutoSize = true,
            MaximumSize = new Size(590, 0),
            Text = "只填写新建的最小权限 CAM 子账号凭据。聊天中曾出现的旧 SecretId 和 SecretKey 已暴露，必须先在腾讯云控制台撤销，绝不能在此继续使用。\r\n\r\n软件会在上海区域创建私有 COS 专用桶，把随机命名的临时音频上传到腾讯云，调用常规 16k_zh 录音文件识别，并在成功或最终失败后立即删除对象。专用前缀设置 1 天生命周期和未完成分块清理，相关 COS 请求、存储和生命周期删除可能产生少量费用。普通录音识别当前有每月 10 小时免费额度，但其他电脑、软件和控制台用量无法由本机得知；腾讯云控制台是唯一权威数据。服务是后付费，超出免费额度可能收费。保存配置不会上传测试音频或消耗识别额度。\r\n\r\n凭据仅以 Windows DPAPI CurrentUser 加密保存在本机，每台电脑都要重新输入；不会写入日志、Git 或交接说明。",
            AccessibleName = "腾讯云隐私费用和凭据安全说明"
        };
        layout.SetColumnSpan(notice, 2);
        layout.Controls.Add(notice, 0, 0);

        ConfigureInput(appIdBox, "腾讯云 AppID");
        ConfigureInput(secretIdBox, "腾讯云 SecretId");
        ConfigureInput(secretKeyBox, "腾讯云 SecretKey");
        secretKeyBox.UseSystemPasswordChar = true;
        layout.Controls.Add(new Label { Text = "AppID：", AutoSize = true, Anchor = AnchorStyles.Left }, 0, 1);
        layout.Controls.Add(appIdBox, 1, 1);
        layout.Controls.Add(new Label { Text = "SecretId：", AutoSize = true, Anchor = AnchorStyles.Left }, 0, 2);
        layout.Controls.Add(secretIdBox, 1, 2);
        layout.Controls.Add(new Label { Text = "SecretKey：", AutoSize = true, Anchor = AnchorStyles.Left }, 0, 3);
        layout.Controls.Add(secretKeyBox, 1, 3);

        var showSecret = new CheckBox
        {
            Text = "暂时显示 SecretKey",
            AutoSize = true,
            AccessibleName = "暂时显示 SecretKey"
        };
        showSecret.CheckedChanged += (_, _) => secretKeyBox.UseSystemPasswordChar = !showSecret.Checked;
        layout.SetColumnSpan(showSecret, 2);
        layout.Controls.Add(showSecret, 0, 4);

        acknowledgeBox.Text = "我已撤销暴露的旧密钥，并理解隐私、ASR 后付费和 COS 费用风险";
        acknowledgeBox.AutoSize = true;
        acknowledgeBox.AccessibleName = acknowledgeBox.Text;
        acknowledgeBox.CheckedChanged += (_, _) => UpdateSaveEnabled();
        layout.SetColumnSpan(acknowledgeBox, 2);
        layout.Controls.Add(acknowledgeBox, 0, 5);

        var buttons = new FlowLayoutPanel
        {
            AutoSize = true,
            FlowDirection = FlowDirection.RightToLeft,
            Dock = DockStyle.Fill,
            Margin = new Padding(0, 12, 0, 0)
        };
        var cancel = new Button { Text = "取消", AutoSize = true, DialogResult = DialogResult.Cancel };
        saveButton.Text = "仅保存配置";
        saveButton.AutoSize = true;
        saveButton.Enabled = false;
        saveButton.DialogResult = DialogResult.OK;
        buttons.Controls.Add(cancel);
        buttons.Controls.Add(saveButton);
        layout.SetColumnSpan(buttons, 2);
        layout.Controls.Add(buttons, 0, 6);

        Controls.Add(layout);
        AcceptButton = saveButton;
        CancelButton = cancel;
        appIdBox.TextChanged += (_, _) => UpdateSaveEnabled();
        secretIdBox.TextChanged += (_, _) => UpdateSaveEnabled();
        secretKeyBox.TextChanged += (_, _) => UpdateSaveEnabled();
        Shown += (_, _) => appIdBox.Focus();
    }

    public CloudCredentials Credentials => new(
        appIdBox.Text.Trim(),
        secretIdBox.Text.Trim(),
        secretKeyBox.Text);

    private static void ConfigureInput(TextBox textBox, string accessibleName)
    {
        textBox.Dock = DockStyle.Fill;
        textBox.AccessibleName = accessibleName;
    }

    private void UpdateSaveEnabled()
    {
        saveButton.Enabled = acknowledgeBox.Checked &&
            appIdBox.Text.Trim().Length > 0 &&
            secretIdBox.Text.Trim().Length > 0 &&
            secretKeyBox.Text.Length > 0;
    }
}
