-- MySQL dump 10.19  Distrib 10.5.23-MariaDB
--
-- Host: db1234.eqiad.wmnet    Database: simplewiki
-- ------------------------------------------------------
-- Server version	10.5.23-MariaDB-log
--
-- A hand-built miniature of the real `page` dump. Deliberately nasty: see
-- fixtures/README.md. Keep it small and keep the expectations in
-- pipeline/tests/test_sqldump.py in step with it.

/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
DROP TABLE IF EXISTS `page`;
CREATE TABLE `page` (
  `page_id` int(10) unsigned NOT NULL AUTO_INCREMENT,
  `page_namespace` int(11) NOT NULL DEFAULT 0,
  `page_title` varbinary(255) NOT NULL,
  `page_is_redirect` tinyint(3) unsigned NOT NULL DEFAULT 0,
  `page_is_new` tinyint(3) unsigned NOT NULL DEFAULT 0,
  `page_random` double unsigned NOT NULL DEFAULT 0,
  `page_touched` binary(14) NOT NULL,
  `page_links_updated` varbinary(14) DEFAULT NULL,
  `page_latest` int(10) unsigned NOT NULL DEFAULT 0,
  `page_len` int(10) unsigned NOT NULL DEFAULT 0,
  `page_content_model` varbinary(32) DEFAULT NULL,
  `page_lang` varbinary(35) DEFAULT NULL,
  PRIMARY KEY (`page_id`),
  UNIQUE KEY `page_name_title` (`page_namespace`,`page_title`),
  KEY `page_random` (`page_random`),
  KEY `page_len` (`page_len`),
  KEY `page_redirect_namespace_len` (`page_is_redirect`,`page_namespace`,`page_len`)
) ENGINE=InnoDB AUTO_INCREMENT=30 DEFAULT CHARSET=binary ROW_FORMAT=COMPRESSED;
/*!40101 SET character_set_client = @saved_cs_client */;

INSERT INTO `page` VALUES (10,0,'AccessibleComputing',1,0,0.33167112649,'20260101000000','20260101000000',123,94,'wikitext',NULL),(12,0,'Anarchism',0,0,0.786172332,'20260102000000','20260102000000',456,12345,'wikitext',NULL),(13,0,'Alien_(film)',0,0,0.1234,'20260103000000',NULL,457,2048,'wikitext',NULL),(14,0,'Smith,_John',0,0,0.2345,'20260104000000',NULL,458,1024,'wikitext',NULL),(15,0,'It\'s_a_Wonderful_Life',0,0,0.3456,'20260105000000',NULL,459,3072,'wikitext',NULL),(16,0,'Say_\"Hello\"',0,0,0.4567,'20260106000000',NULL,460,512,'wikitext',NULL),(17,0,'Back\\slash',0,0,0.5678,'20260107000000',NULL,461,256,'wikitext',NULL);
INSERT INTO `page` VALUES (18,0,'Trailing\\',0,0,0.6789,'20260108000000',NULL,462,128,'wikitext',NULL),(19,0,'Line\nBreak',0,0,0.789,'20260109000000',NULL,463,64,'wikitext',NULL),(20,0,'Fifty\%_Off',0,0,0.8901,'20260110000000',NULL,464,32,'wikitext',NULL),(21,0,'Doubled''Quote',0,0,0.9012,'20260111000000',NULL,465,16,'wikitext',NULL),(22,0,'Zürich',0,0,0.1111,'20260112000000',NULL,466,8192,'wikitext',NULL),(23,0,'日本',0,0,0.2222,'20260113000000',NULL,467,4096,'wikitext',NULL),(24,1,'Talk_target',0,0,0.3333,'20260114000000',NULL,468,100,'wikitext',NULL);
INSERT INTO `page` VALUES (25,10,'Infobox',0,0,0.4444,'20260115000000',NULL,469,200,'wikitext',NULL),(26,14,'Films',0,0,0.5555,'20260116000000',NULL,470,300,'wikitext',NULL),(27,0,'Semicolon;_Test',0,0,0.6666,'20260117000000',NULL,471,400,'wikitext',NULL),(28,0,'Null_And_Negative',0,0,0.7777,'20260118000000',NULL,-1,0,NULL,NULL),(29,0,'Hex_Touched',0,0,0.8888,0x3230323630313139303030303030,NULL,473,500,'wikitext','en-gb');
