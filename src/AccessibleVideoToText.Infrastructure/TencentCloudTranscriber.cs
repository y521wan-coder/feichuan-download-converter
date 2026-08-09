using System.Globalization;
using AccessibleVideoToText.Core;
using TencentCloud.Asr.V20190614;
using TencentCloud.Asr.V20190614.Models;
using TencentCloud.Common;
using TencentCloud.Common.Profile;

namespace AccessibleVideoToText.Infrastructure;

public sealed class TencentCloudTranscriber : ICloudTranscriber
{
    private readonly AsrClient client;

    public TencentCloudTranscriber(CloudCredentials credentials)
    {
        ArgumentNullException.ThrowIfNull(credentials);
        var credential = new Credential
        {
            SecretId = credentials.SecretId,
            SecretKey = credentials.SecretKey
        };
        var httpProfile = new HttpProfile
        {
            Endpoint = CloudRequestPolicy.Locked.Endpoint,
            ReqMethod = HttpProfile.REQ_POST,
            Protocol = HttpProfile.REQ_HTTPS,
            Timeout = 60
        };
        var clientProfile = new ClientProfile(ClientProfile.SIGN_TC3SHA256, httpProfile);
        client = new AsrClient(credential, CloudRequestPolicy.Locked.CosRegion, clientProfile);
    }

    public async Task<string> SubmitAsync(Uri signedAudioUrl, CancellationToken cancellationToken)
    {
        var request = TencentAsrRequestFactory.Create(signedAudioUrl);
        var response = await client.CreateRecTask(request).WaitAsync(cancellationToken).ConfigureAwait(false);
        var taskId = response.Data?.TaskId;
        if (taskId is null)
        {
            throw new InvalidOperationException("腾讯云没有返回录音识别任务编号。 ");
        }

        return taskId.Value.ToString(CultureInfo.InvariantCulture);
    }

    public async Task<CloudTranscriptionStatus> GetStatusAsync(string taskId, CancellationToken cancellationToken)
    {
        if (!ulong.TryParse(taskId, NumberStyles.None, CultureInfo.InvariantCulture, out var parsedTaskId))
        {
            throw new ArgumentException("腾讯云任务编号无效。", nameof(taskId));
        }

        var response = await client.DescribeTaskStatus(new DescribeTaskStatusRequest { TaskId = parsedTaskId })
            .WaitAsync(cancellationToken)
            .ConfigureAwait(false);
        var data = response.Data ?? throw new InvalidOperationException("腾讯云没有返回录音识别任务状态。 ");

        return data.Status switch
        {
            0 => new CloudTranscriptionStatus(CloudTranscriptionState.Waiting, null, null, null),
            1 => new CloudTranscriptionStatus(CloudTranscriptionState.Running, null, null, null),
            2 => BuildSuccess(data),
            3 => new CloudTranscriptionStatus(
                CloudTranscriptionState.Failed,
                null,
                data.StatusStr ?? "failed",
                string.IsNullOrWhiteSpace(data.ErrorMsg) ? "腾讯云录音识别失败。" : data.ErrorMsg),
            _ => new CloudTranscriptionStatus(
                CloudTranscriptionState.Failed,
                null,
                "unknown-status",
                $"腾讯云返回了未知任务状态：{data.Status?.ToString(CultureInfo.InvariantCulture) ?? "空"}。")
        };
    }

    private static CloudTranscriptionStatus BuildSuccess(TencentCloud.Asr.V20190614.Models.TaskStatus data)
    {
        var body = TencentAsrResultParser.ExtractBody(data);
        return string.IsNullOrWhiteSpace(body)
            ? new CloudTranscriptionStatus(
                CloudTranscriptionState.Failed,
                null,
                "empty-transcript",
                "腾讯云任务成功，但没有识别到有效语音，因此不会生成空 TXT。")
            : new CloudTranscriptionStatus(CloudTranscriptionState.Succeeded, body, null, null);
    }
}
