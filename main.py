import os
import argparse
import logging
from dotenv import load_dotenv

from mock_generator import generate_mock_ticket_data_chunked
from address_normalizer import normalize_addresses
from data_hasher import apply_data_hashing
from fraud_detector import TicketFraudDetector
from neo4j.exceptions import Neo4jError

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def main():
    parser = argparse.ArgumentParser(description="암표 탐지 데이터 전처리 및 Neo4j 그래프 적재 파이프라인")
    parser.add_argument("--all", action="store_true", help="데이터 생성부터 탐지까지 전체 파이프라인을 한 번에 실행합니다.")
    parser.add_argument("--generate", action="store_true", help="10만 건의 모의 예매 데이터를 생성합니다.")
    parser.add_argument("--normalize", action="store_true", help="생성된 데이터의 주소를 정규화합니다.")
    parser.add_argument("--hash", action="store_true", help="정규화된 데이터를 비식별화(해싱)하고 원본을 파기합니다.")
    parser.add_argument("--detect", action="store_true", help="Neo4j에 데이터를 적재하고 암표 조직을 탐지합니다.")
    
    args = parser.parse_args()
    
    if not any([args.all, args.generate, args.normalize, args.hash, args.detect]):
        args.all = True

    MOCK_FILE = "mock_ticket_data.csv"
    NORMALIZED_FILE = "normalized_ticket_data.csv"
    SECURE_FILE = "final_secure_ticket_data.csv"

    logger.info("=== 암표 탐지 파이프라인 에이전트를 가동합니다 ===")

    try:
        if args.all or args.generate:
            logger.info("대규모 모의 데이터 생성을 시작합니다")
            generate_mock_ticket_data_chunked(
                total_records=100000, 
                abuse_ratio=0.05, 
                output_file=MOCK_FILE
            )

        if args.all or args.normalize:
            logger.info("Jaro-Winkler 기반 주소 정규화를 시작합니다")
            normalize_addresses(MOCK_FILE, NORMALIZED_FILE)

        if args.all or args.hash:
            logger.info("로컬 메모리 비식별화 및 평문 파기를 시작합니다")
            apply_data_hashing(
                input_filepath=NORMALIZED_FILE, 
                output_filepath=SECURE_FILE, 
                columns_to_hash=['phone', 'payment_method', 'normalized_address'],
                use_fast_mock_mode=True
            )

        if args.all or args.detect:
            logger.info("Neo4j 그래프 DB 적재 및 어뷰징 탐지를 시작합니다.")
            neo4j_password = os.getenv("NEO4J_PASSWORD")
            if not neo4j_password:
                raise ValueError(".env 파일에 NEO4J_PASSWORD가 설정되지 않았습니다.")
                
            fraud_detector = TicketFraudDetector("bolt://localhost:7687", "neo4j", neo4j_password)
            try:
                fraud_detector.create_indexes()
                fraud_detector.load_csv_to_graph()
                
                suspicious_clusters = fraud_detector.detect_abnormal_payment_clusters(threshold=5)
                if suspicious_clusters:
                    logger.info(f"암표 의심 군집 {len(suspicious_clusters)}건이 성공적으로 적발되었습니다!")
                else:
                    logger.info("탐지된 이상 결제 군집이 없습니다.")
            finally:
                fraud_detector.close()

        logger.info("=== 모든 파이프라인 처리가 성공적으로 종료되었습니다 ===")

    except Neo4jError as db_err:
        logger.error(f"Neo4j 데이터베이스 처리 중 에러 발생: {db_err}")
    except Exception as e:
        logger.error(f"파이프라인 실행 중 치명적 오류 발생: {e}")

if __name__ == "__main__":
    main()