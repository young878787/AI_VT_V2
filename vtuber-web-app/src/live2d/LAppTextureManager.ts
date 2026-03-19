/**
 * Copyright(c) Live2D Inc. All rights reserved.
 *
 * Use of this source code is governed by the Live2D Open Software license
 * that can be found at https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html.
 */

/**
 * 紋理資訊結構
 */
export class TextureInfo {
  public img: HTMLImageElement | null = null; // 圖像
  public id: WebGLTexture | null = null; // 紋理
  public width = 0; // 寬度
  public height = 0; // 高度
  public usePremultiply = false; // Premultiply 處理是否有效
  public fileName = ''; // 文件名
}

/**
 * 紋理管理類
 * 處理圖像載入與管理
 */
export class LAppTextureManager {
  private _textures: TextureInfo[] = [];
  private _gl: WebGLRenderingContext | WebGL2RenderingContext | null = null;

  /**
   * 建構函式
   */
  public constructor(gl?: WebGLRenderingContext | WebGL2RenderingContext) {
    this._gl = gl || null;
  }

  /**
   * 設定 WebGL context
   */
  public setGl(gl: WebGLRenderingContext | WebGL2RenderingContext): void {
    this._gl = gl;
  }

  /**
   * 取得 WebGL context
   */
  public getGl(): WebGLRenderingContext | WebGL2RenderingContext | null {
    return this._gl;
  }

  /**
   * 釋放資源
   */
  public release(): void {
    if (!this._gl) return;

    for (let i = 0; i < this._textures.length; i++) {
      if (this._textures[i].id) {
        this._gl.deleteTexture(this._textures[i].id);
      }
    }
    this._textures = [];
  }

  /**
   * 載入圖片並創建紋理
   * @param fileName 圖片文件名
   * @param usePremultiply 是否使用 Premultiply 處理
   * @returns 紋理資訊
   */
  public createTextureFromPngFile(
    fileName: string,
    usePremultiply: boolean,
    callback: (textureInfo: TextureInfo) => void
  ): void {
    if (!this._gl) {
      console.error('WebGL context 未設置');
      return;
    }

    // 先搜尋已載入的紋理
    for (let i = 0; i < this._textures.length; i++) {
      if (this._textures[i].fileName === fileName) {
        // 已載入，直接返回
        callback(this._textures[i]);
        return;
      }
    }

    // 創建新的紋理資訊
    const textureInfo = new TextureInfo();
    textureInfo.fileName = fileName;
    textureInfo.usePremultiply = usePremultiply;

    // 載入圖像
    const img = new Image();
    img.crossOrigin = 'anonymous';

    img.onload = () => {
      // 創建紋理物件
      const tex = this._gl!.createTexture();
      if (!tex) {
        console.error('無法創建紋理');
        return;
      }

      // 綁定紋理
      this._gl!.bindTexture(this._gl!.TEXTURE_2D, tex);

      // 設定紋理參數
      this._gl!.texParameteri(
        this._gl!.TEXTURE_2D,
        this._gl!.TEXTURE_MIN_FILTER,
        this._gl!.LINEAR_MIPMAP_LINEAR
      );
      this._gl!.texParameteri(
        this._gl!.TEXTURE_2D,
        this._gl!.TEXTURE_MAG_FILTER,
        this._gl!.LINEAR
      );

      // Premultiply 處理
      if (usePremultiply) {
        this._gl!.pixelStorei(this._gl!.UNPACK_PREMULTIPLY_ALPHA_WEBGL, 1);
      }

      // 上傳紋理
      this._gl!.texImage2D(
        this._gl!.TEXTURE_2D,
        0,
        this._gl!.RGBA,
        this._gl!.RGBA,
        this._gl!.UNSIGNED_BYTE,
        img
      );

      // 生成 mipmap
      this._gl!.generateMipmap(this._gl!.TEXTURE_2D);

      // 恢復 Premultiply 設定
      if (usePremultiply) {
        this._gl!.pixelStorei(this._gl!.UNPACK_PREMULTIPLY_ALPHA_WEBGL, 0);
      }

      // 解綁紋理
      this._gl!.bindTexture(this._gl!.TEXTURE_2D, null);

      // 設定紋理資訊
      textureInfo.id = tex;
      textureInfo.width = img.width;
      textureInfo.height = img.height;
      textureInfo.img = img;

      // 添加到列表
      this._textures.push(textureInfo);

      callback(textureInfo);
    };

    img.onerror = () => {
      console.error(`無法載入圖片：${fileName}`);
    };

    img.src = fileName;
  }

  /**
   * 釋放指定紋理
   * @param textureInfo 紋理資訊
   */
  public releaseTexture(textureInfo: TextureInfo | null): void {
    if (!textureInfo || !this._gl) return;

    for (let i = 0; i < this._textures.length; i++) {
      if (this._textures[i].id === textureInfo.id) {
        if (this._textures[i].id) {
          this._gl.deleteTexture(this._textures[i].id);
        }
        this._textures.splice(i, 1);
        return;
      }
    }
  }

  /**
   * 釋放所有紋理
   */
  public releaseAllTextures(): void {
    this.release();
  }
}
