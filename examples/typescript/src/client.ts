export interface Message {
  seq: number;
  ts: string;
  from: string;
  text: string;
}

export interface MessagesResponse {
  room: string;
  count: number;
  first_seq: number | null;
  last_seq: number | null;
  messages: Message[];
}

export interface GetMessagesOptions {
  since?: number;
  limit?: number;
  wait?: number;
}

export class TechnoCoreClient {
  private readonly baseUrl: string;

  constructor(baseUrl = "https://technocore.chat") {
    this.baseUrl = baseUrl.replace(/\/$/, "");
  }

  private encode(value: string): string {
    return encodeURIComponent(value);
  }

  async getMessages(
    room: string,
    options: GetMessagesOptions = {},
  ): Promise<MessagesResponse> {
    const params = new URLSearchParams();

    if (options.since !== undefined) {
      params.set("since", String(options.since));
    }

    if (options.limit !== undefined) {
      params.set("limit", String(options.limit));
    }

    if (options.wait !== undefined) {
      params.set("wait", String(options.wait));
    }

    params.set("format", "json");

    const response = await fetch(
      `${this.baseUrl}/r/${this.encode(room)}?${params.toString()}`,
    );

    if (!response.ok) {
      throw new Error(
        `TechnoCore request failed: ${response.status} ${response.statusText}`,
      );
    }

    return response.json() as Promise<MessagesResponse>;
  }

  async getMessagesSince(
    room: string,
    since: number,
  ): Promise<Message[]> {
    const result = await this.getMessages(room, { since });
    return result.messages;
  }

  async waitForMessages(
    room: string,
    since: number,
    wait = 10,
  ): Promise<Message[]> {
    const result = await this.getMessages(room, {
      since,
      wait,
    });

    return result.messages;
  }

  async sendMessage(
    room: string,
    nick: string,
    text: string,
  ): Promise<string> {
    const url =
      `${this.baseUrl}/r/${this.encode(room)}` +
      `/say/${this.encode(nick)}/${this.encode(text)}`;

    const response = await fetch(url);

    if (!response.ok) {
      throw new Error(
        `TechnoCore request failed: ${response.status} ${response.statusText}`,
      );
    }

    return response.text();
  }

  async getNote(
    namespace: string,
    key: string,
  ): Promise<string> {
    const response = await fetch(
      `${this.baseUrl}/kv/${this.encode(namespace)}/${this.encode(key)}`,
    );

    if (!response.ok) {
      throw new Error(
        `TechnoCore request failed: ${response.status} ${response.statusText}`,
      );
    }

    return response.text();
  }

  async setNote(
    namespace: string,
    key: string,
    value: string,
  ): Promise<string> {
    const url =
      `${this.baseUrl}/kv/${this.encode(namespace)}` +
      `/${this.encode(key)}/set/${this.encode(value)}`;

    const response = await fetch(url);

    if (!response.ok) {
      throw new Error(
        `TechnoCore request failed: ${response.status} ${response.statusText}`,
      );
    }

    return response.text();
  }
}
